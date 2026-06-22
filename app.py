import os
import io
import json
import time
import shutil
import socket
import hashlib
import tarfile
import logging
import threading
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from flask import Flask, render_template, jsonify, request, send_file, session, redirect
from werkzeug.utils import secure_filename
from PIL import Image

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.environ.get('MAP_DATA_DIR', _BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(DATA_DIR, 'map.log')),
    ],
)
log = logging.getLogger(__name__)

BACKUPS_DIR    = os.path.join(DATA_DIR, 'backups')
ICONS_DIR      = os.path.join(DATA_DIR, 'icons')
SCHEDULES_FILE = os.path.join(DATA_DIR, 'schedules.json')
AUTH_FILE      = os.path.join(DATA_DIR, 'auth.json')
os.makedirs(BACKUPS_DIR, exist_ok=True)
os.makedirs(ICONS_DIR, exist_ok=True)

ALLOWED_ICON_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
MAX_ICON_SIZE = 2 * 1024 * 1024  # 2 MB

# ── Auth helpers ────────────────────────────────────────────────────────────

def _hash_password(password: str, salt: str = None):
    if salt is None:
        salt = os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100_000).hex()
    return h, salt

def _verify_password(password: str, stored_hash: str, salt: str) -> bool:
    h, _ = _hash_password(password, salt)
    return h == stored_hash

def _load_auth() -> dict:
    with open(AUTH_FILE) as f:
        return json.load(f)

def _init_auth():
    if os.path.exists(AUTH_FILE):
        return
    pw_hash, salt = _hash_password('admin')
    auth = {
        'username': 'admin',
        'password_hash': pw_hash,
        'salt': salt,
        'secret_key': os.urandom(32).hex(),
    }
    with open(AUTH_FILE, 'w') as f:
        json.dump(auth, f, indent=2)
    log.warning('No auth config found — created default credentials admin/admin. Change your password immediately.')

_init_auth()
app = Flask(__name__)
app.config['SECRET_KEY'] = _load_auth().get('secret_key', 'mc-panel-fallback')

# Tracks in-progress / recently finished server creations: {name: {'status': 'pending'|'done'|'error', 'error': str}}
creation_tasks: dict = {}

# Simple version cache: {server_type: (timestamp, [versions])}
_version_cache: dict = {}
_VERSION_CACHE_TTL = 300  # seconds

_schedules_lock = threading.Lock()

# ── Docker helpers ──────────────────────────────────────────────────────────

def docker_run(args, capture=True, timeout=30, quiet=False):
    cmd = ['docker'] + args
    try:
        result = subprocess.run(
            cmd, capture_output=capture,
            text=True, timeout=timeout
        )
        if result.returncode != 0 and not quiet:
            log.warning('docker %s failed (rc=%d): %s', args[0], result.returncode, result.stderr.strip())
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        log.error('docker %s timed out after %ds', args[0], timeout)
        return 1, '', 'Command timed out'
    except FileNotFoundError:
        log.error('docker executable not found')
        return 1, '', 'Docker not found'

def get_containers():
    code, out, err = docker_run([
        'ps', '-a',
        '--filter', 'label=mc-panel=true',
        '--format', '{{json .}}'
    ])
    containers = []
    if code == 0 and out:
        for line in out.splitlines():
            try:
                containers.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return containers

def get_stats(container_id):
    code, out, err = docker_run([
        'stats', container_id,
        '--no-stream',
        '--format', '{{json .}}'
    ])
    if code == 0 and out:
        try:
            return json.loads(out.splitlines()[0])
        except (json.JSONDecodeError, IndexError):
            pass
    return {}

def send_rcon(container_id, command):
    """Send a command via docker exec to the Minecraft RCON/stdin."""
    code, out, err = docker_run([
        'exec', '-i', container_id,
        'rcon-cli', command
    ])
    if code != 0:
        # fallback: write to container stdin via attach
        code, out, err = docker_run([
            'exec', container_id,
            'sh', '-c', f'echo "{command}" > /proc/1/fd/0'
        ])
    return code, out or err

# ── Version fetching ────────────────────────────────────────────────────────

def _fetch_versions(server_type: str) -> list:
    try:
        if server_type == 'PAPER':
            with urllib.request.urlopen('https://fill.papermc.io/v3/projects/paper', timeout=10) as r:
                data = json.loads(r.read())
            groups = data.get('versions', {})
            return [v for group in groups.values() for v in group]

        if server_type == 'PURPUR':
            with urllib.request.urlopen('https://api.purpurmc.org/v2/purpur', timeout=10) as r:
                return list(reversed(json.loads(r.read()).get('versions', [])))

        if server_type == 'FABRIC':
            with urllib.request.urlopen('https://meta.fabricmc.net/v2/versions/game', timeout=10) as r:
                data = json.loads(r.read())
            return [v['version'] for v in data if v.get('stable')]

        # VANILLA / FORGE / SPIGOT — use Mojang release manifest
        with urllib.request.urlopen(
            'https://launchermeta.mojang.com/mc/game/version_manifest_v2.json', timeout=10
        ) as r:
            data = json.loads(r.read())
        return [v['id'] for v in data.get('versions', []) if v['type'] == 'release']

    except Exception as exc:
        log.warning('Failed to fetch versions for %s: %s', server_type, exc)
        return []

# ── Routes ──────────────────────────────────────────────────────────────────

@app.before_request
def require_login():
    if request.endpoint in ('login', 'static'):
        return
    if not session.get('logged_in'):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Unauthorized'}), 401
        return redirect('/login')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        if session.get('logged_in'):
            return redirect('/')
        return render_template('login.html')
    data = request.json or {}
    auth = _load_auth()
    if (data.get('username', '').strip() == auth['username'] and
            _verify_password(data.get('password', ''), auth['password_hash'], auth['salt'])):
        session['logged_in'] = True
        log.info('Admin logged in from %s', request.remote_addr)
        return jsonify({'success': True})
    log.warning('Failed login attempt from %s', request.remote_addr)
    return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})

@app.route('/api/change-password', methods=['POST'])
def change_password():
    data = request.json or {}
    current = data.get('current', '')
    new_pw = data.get('new_password', '')
    if len(new_pw) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400
    auth = _load_auth()
    if not _verify_password(current, auth['password_hash'], auth['salt']):
        return jsonify({'error': 'Current password is incorrect'}), 401
    pw_hash, salt = _hash_password(new_pw)
    auth['password_hash'] = pw_hash
    auth['salt'] = salt
    with open(AUTH_FILE, 'w') as f:
        json.dump(auth, f, indent=2)
    log.info('Admin password changed')
    return jsonify({'success': True})

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/versions')
def get_versions():
    server_type = request.args.get('type', 'PAPER').upper()
    now = time.time()
    if server_type in _version_cache:
        ts, versions = _version_cache[server_type]
        if now - ts < _VERSION_CACHE_TTL:
            return jsonify(versions)
    versions = _fetch_versions(server_type)
    _version_cache[server_type] = (now, versions)
    return jsonify(versions)

@app.route('/api/servers', methods=['GET'])
def list_servers():
    containers = get_containers()
    servers = []
    for c in containers:
        cid = c.get('ID', c.get('Id', ''))[:12]
        name = c.get('Names', c.get('Name', 'unknown')).lstrip('/')
        status = c.get('Status', c.get('State', 'unknown'))
        ports = c.get('Ports', '')
        
        # Extract port mapping
        port = None
        if ports:
            import re
            m = re.search(r'0\.0\.0\.0:(\d+)->25565', ports)
            if m:
                port = int(m.group(1))

        # Get labels for metadata
        code, label_out, _ = docker_run([
            'inspect', cid,
            '--format', '{{json .Config.Labels}}'
        ])
        labels = {}
        if code == 0 and label_out:
            try:
                labels = json.loads(label_out)
            except json.JSONDecodeError:
                pass

        servers.append({
            'id': cid,
            'name': name,
            'status': status,
            'port': port,
            'version': labels.get('mc-version', 'unknown'),
            'type': labels.get('mc-type', 'VANILLA'),
            'memory': labels.get('mc-memory', '2G'),
            'motd': labels.get('mc-motd', ''),
            'max_players': labels.get('mc-max-players', '20'),
            'created': c.get('CreatedAt', c.get('Created', '')),
            'has_icon': _icon_path(name) is not None,
        })
    return jsonify(servers)

def _apply_whitelist_when_ready(container_name: str, players: list):
    """Wait for RCON to be available then apply whitelist via commands."""
    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(15)
        code, _, _ = docker_run(
            ['exec', container_name, 'rcon-cli', '--host', '127.0.0.1', '--password', 'mcpanel', 'list'],
            quiet=True, timeout=5
        )
        if code == 0:
            for player in players:
                docker_run(
                    ['exec', container_name, 'rcon-cli', '--host', '127.0.0.1', '--password', 'mcpanel',
                     f'whitelist add {player}'],
                    quiet=True
                )
            docker_run(
                ['exec', container_name, 'rcon-cli', '--host', '127.0.0.1', '--password', 'mcpanel', 'whitelist on'],
                quiet=True
            )
            log.info('Whitelist applied on %s: %s', container_name, players)
            return
    log.warning('Timed out waiting for RCON on %s to apply whitelist', container_name)

def _create_server_task(name, server_type, version, memory, port, difficulty, max_players, motd, whitelist):
    volume_name = f'mc-{name}-data'
    docker_run(['volume', 'create', volume_name])

    cmd = [
        'run', '-d',
        '--name', name,
        '-p', f'{port}:25565',
        '-p', f'{port+1}:25575',
        '--memory', memory,
        '-e', 'EULA=TRUE',
        '-e', f'VERSION={version}',
        '-e', f'TYPE={server_type}',
        '-e', f'MEMORY={memory}',
        '-e', f'DIFFICULTY={difficulty}',
        '-e', f'MAX_PLAYERS={max_players}',
        '-e', f'MOTD={motd}',
        '-e', 'RCON_PASSWORD=mcpanel',
        '-e', 'ENABLE_RCON=true',
        '-e', 'ONLINE_MODE=TRUE',
        '-v', f'{volume_name}:/data',
        '--label', 'mc-panel=true',
        '--label', f'mc-version={version}',
        '--label', f'mc-type={server_type}',
        '--label', f'mc-memory={memory}',
        '--label', f'mc-motd={motd}',
        '--label', f'mc-max-players={max_players}',
        '--restart', 'unless-stopped',
    ]
    cmd.append('itzg/minecraft-server')

    log.info('Pulling image and starting container for %s (may take a few minutes on first run)…', name)
    code, out, err = docker_run(cmd, timeout=300)

    if code != 0:
        log.error('Failed to create server %s: %s', name, err)
        creation_tasks[name] = {'status': 'error', 'error': err or 'Failed to create container'}
        return

    log.info('Server created name=%s id=%s', name, out[:12])
    creation_tasks[name] = {'status': 'done', 'id': out[:12]}

    if whitelist:
        players = [p.strip() for p in whitelist.split(',') if p.strip()]
        t = threading.Thread(target=_apply_whitelist_when_ready, args=(name, players), daemon=True)
        t.start()

@app.route('/api/servers', methods=['POST'])
def create_server():
    data = request.json
    name = data.get('name', '').strip()
    version = data.get('version', 'LATEST')
    server_type = data.get('type', 'PAPER')
    memory = data.get('memory', '2G')
    port = int(data.get('port', 25565))
    difficulty = data.get('difficulty', 'normal')
    max_players = data.get('max_players', 20)
    motd = data.get('motd', 'A Minecraft Server')
    whitelist = data.get('whitelist', '').strip()

    if not name:
        return jsonify({'error': 'Name is required'}), 400

    if name in creation_tasks and creation_tasks[name]['status'] == 'pending':
        return jsonify({'error': f'Server "{name}" is already being created'}), 409

    log.info('Creating server name=%s type=%s version=%s port=%d memory=%s', name, server_type, version, port, memory)

    # Check port not in use
    code, out, _ = docker_run(['ps', '--filter', f'publish={port}', '--format', '{{.Names}}'])
    if code == 0 and out:
        log.warning('Port %d already in use by %s', port, out)
        return jsonify({'error': f'Port {port} is already in use'}), 400

    creation_tasks[name] = {'status': 'pending'}
    t = threading.Thread(target=_create_server_task,
                         args=(name, server_type, version, memory, port, difficulty, max_players, motd, whitelist),
                         daemon=True)
    t.start()

    return jsonify({'success': True, 'name': name, 'pending': True})

@app.route('/api/servers/<name>/creation-status', methods=['GET'])
def creation_status(name):
    task = creation_tasks.get(name)
    if not task:
        return jsonify({'status': 'unknown'})
    return jsonify(task)

@app.route('/api/servers/<server_id>/start', methods=['POST'])
def start_server(server_id):
    log.info('Starting server %s', server_id)
    code, out, err = docker_run(['start', server_id])
    if code != 0:
        log.error('Failed to start server %s: %s', server_id, err)
        return jsonify({'error': err}), 500
    return jsonify({'success': True})

@app.route('/api/servers/<server_id>/stop', methods=['POST'])
def stop_server(server_id):
    log.info('Stopping server %s', server_id)
    code, out, err = docker_run(['stop', server_id])
    if code != 0:
        log.error('Failed to stop server %s: %s', server_id, err)
        return jsonify({'error': err}), 500
    return jsonify({'success': True})

@app.route('/api/servers/<server_id>/restart', methods=['POST'])
def restart_server(server_id):
    log.info('Restarting server %s', server_id)
    code, out, err = docker_run(['restart', server_id])
    if code != 0:
        log.error('Failed to restart server %s: %s', server_id, err)
        return jsonify({'error': err}), 500
    return jsonify({'success': True})

def _inspect_container(server_id):
    """Return (info_dict, name, labels, env, port, volume_name) or raise ValueError."""
    code, out, _ = docker_run(['inspect', server_id, '--format', '{{json .}}'])
    if code != 0:
        raise ValueError('Could not inspect container')
    try:
        info = json.loads(out)
    except json.JSONDecodeError:
        raise ValueError('Could not parse container info')

    name = info['Name'].lstrip('/')
    labels = info['Config']['Labels']
    env = {e.split('=', 1)[0]: e.split('=', 1)[1] for e in info['Config']['Env'] if '=' in e}

    port = None
    for container_port, bindings in info['HostConfig']['PortBindings'].items():
        if container_port == '25565/tcp' and bindings:
            port = int(bindings[0]['HostPort'])
            break
    if port is None:
        raise ValueError('Could not determine server port')

    volume_name = next(
        (m['Name'] for m in info['Mounts'] if m['Type'] == 'volume'),
        f'mc-{name}-data'
    )
    return name, labels, env, port, volume_name

def _recreate_container(name, labels, env, port, volume_name, overrides: dict):
    """Stop, remove, and recreate the container applying env/label overrides."""
    merged_env = {**env, **overrides.get('env', {})}
    merged_labels = {**labels, **overrides.get('labels', {})}
    new_memory = merged_env.get('MEMORY', '2048M')

    docker_run(['stop', name])
    docker_run(['rm', name])

    code, out, err = docker_run([
        'run', '-d',
        '--name', name,
        '-p', f'{port}:25565',
        '-p', f'{port + 1}:25575',
        '--memory', new_memory,
        '-e', 'EULA=TRUE',
        '-e', f'VERSION={merged_env.get("VERSION", "LATEST")}',
        '-e', f'TYPE={merged_env.get("TYPE", "PAPER")}',
        '-e', f'MEMORY={new_memory}',
        '-e', f'DIFFICULTY={merged_env.get("DIFFICULTY", "normal")}',
        '-e', f'MAX_PLAYERS={merged_env.get("MAX_PLAYERS", "20")}',
        '-e', f'MOTD={merged_env.get("MOTD", "A Minecraft Server")}',
        '-e', 'RCON_PASSWORD=mcpanel',
        '-e', 'ENABLE_RCON=true',
        '-e', f'ONLINE_MODE={merged_env.get("ONLINE_MODE", "TRUE")}',
        '-v', f'{volume_name}:/data',
        '--label', 'mc-panel=true',
        '--label', f'mc-version={merged_labels.get("mc-version", merged_env.get("VERSION", "LATEST"))}',
        '--label', f'mc-type={merged_labels.get("mc-type", merged_env.get("TYPE", "PAPER"))}',
        '--label', f'mc-memory={new_memory}',
        '--label', f'mc-motd={merged_labels.get("mc-motd", merged_env.get("MOTD", "A Minecraft Server"))}',
        '--label', f'mc-max-players={merged_labels.get("mc-max-players", merged_env.get("MAX_PLAYERS", "20"))}',
        '--restart', 'unless-stopped',
        'itzg/minecraft-server'
    ], timeout=60)
    return code, out, err

@app.route('/api/servers/<server_id>/memory', methods=['POST'])
def update_memory(server_id):
    data = request.json or {}
    try:
        mib = int(data.get('memory_mib', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'memory_mib must be an integer'}), 400
    if mib < 256:
        return jsonify({'error': 'Minimum memory is 256 MiB'}), 400
    new_memory = f'{mib}M'

    try:
        name, labels, env, port, volume_name = _inspect_container(server_id)
    except ValueError as e:
        return jsonify({'error': str(e)}), 500

    log.info('Updating memory for server %s: %s -> %s', name, labels.get('mc-memory', '?'), new_memory)
    code, out, err = _recreate_container(name, labels, env, port, volume_name,
                                         {'env': {'MEMORY': new_memory}, 'labels': {'mc-memory': new_memory}})
    if code != 0:
        log.error('Failed to recreate server %s with memory=%s: %s', name, new_memory, err)
        return jsonify({'error': err or 'Failed to recreate container'}), 500

    log.info('Server %s recreated with memory=%s id=%s', name, new_memory, out[:12])
    return jsonify({'success': True, 'id': out[:12]})

@app.route('/api/servers/<server_id>/version', methods=['POST'])
def update_version(server_id):
    data = request.json or {}
    new_version = data.get('version', '').strip()
    if not new_version:
        return jsonify({'error': 'Version is required'}), 400

    try:
        name, labels, env, port, volume_name = _inspect_container(server_id)
    except ValueError as e:
        return jsonify({'error': str(e)}), 500

    log.info('Updating version for server %s: %s -> %s', name, labels.get('mc-version', '?'), new_version)
    code, out, err = _recreate_container(name, labels, env, port, volume_name,
                                         {'env': {'VERSION': new_version}, 'labels': {'mc-version': new_version}})
    if code != 0:
        log.error('Failed to recreate server %s with version=%s: %s', name, new_version, err)
        return jsonify({'error': err or 'Failed to recreate container'}), 500

    log.info('Server %s recreated with version=%s id=%s', name, new_version, out[:12])
    return jsonify({'success': True, 'id': out[:12]})

@app.route('/api/servers/<server_id>/motd', methods=['POST'])
def update_motd(server_id):
    data = request.json or {}
    new_motd = data.get('motd', '').strip()
    if not new_motd:
        return jsonify({'error': 'MOTD is required'}), 400

    try:
        name, labels, env, port, volume_name = _inspect_container(server_id)
    except ValueError as e:
        return jsonify({'error': str(e)}), 500

    log.info('Updating MOTD for server %s: -> %s', name, new_motd)
    code, out, err = _recreate_container(name, labels, env, port, volume_name,
                                         {'env': {'MOTD': new_motd}, 'labels': {'mc-motd': new_motd}})
    if code != 0:
        log.error('Failed to recreate server %s with new MOTD: %s', name, err)
        return jsonify({'error': err or 'Failed to recreate container'}), 500

    log.info('Server %s recreated with new MOTD id=%s', name, out[:12])
    return jsonify({'success': True, 'id': out[:12]})

@app.route('/api/servers/<server_id>/max-players', methods=['POST'])
def update_max_players(server_id):
    data = request.json or {}
    try:
        max_players = int(data.get('max_players', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'max_players must be an integer'}), 400
    if max_players < 1:
        return jsonify({'error': 'max_players must be at least 1'}), 400

    try:
        name, labels, env, port, volume_name = _inspect_container(server_id)
    except ValueError as e:
        return jsonify({'error': str(e)}), 500

    new_max = str(max_players)
    log.info('Updating max players for server %s: -> %s', name, new_max)
    code, out, err = _recreate_container(name, labels, env, port, volume_name,
                                         {'env': {'MAX_PLAYERS': new_max}, 'labels': {'mc-max-players': new_max}})
    if code != 0:
        log.error('Failed to recreate server %s with max_players=%s: %s', name, new_max, err)
        return jsonify({'error': err or 'Failed to recreate container'}), 500

    log.info('Server %s recreated with max_players=%s id=%s', name, new_max, out[:12])
    return jsonify({'success': True, 'id': out[:12]})

@app.route('/api/servers/<server_id>/delete', methods=['DELETE'])
def delete_server(server_id):
    # Collect volume names before removing the container
    code, inspect_out, _ = docker_run(['inspect', server_id, '--format', '{{json .Mounts}}'])
    volume_names = []
    if code == 0 and inspect_out:
        try:
            mounts = json.loads(inspect_out)
            volume_names = [m['Name'] for m in mounts if m.get('Type') == 'volume' and m.get('Name')]
        except (json.JSONDecodeError, KeyError):
            pass

    log.info('Deleting server %s (volumes: %s)', server_id, volume_names or 'none')
    docker_run(['stop', server_id])
    code, out, err = docker_run(['rm', '-f', server_id])
    if code != 0:
        log.error('Failed to delete server %s: %s', server_id, err)
        return jsonify({'error': err}), 500

    for vol in volume_names:
        vcode, _, verr = docker_run(['volume', 'rm', vol])
        if vcode != 0:
            log.warning('Failed to remove volume %s: %s', vol, verr)
        else:
            log.info('Removed volume %s', vol)

    log.info('Server deleted %s', server_id)
    return jsonify({'success': True})

@app.route('/api/servers/<server_id>/stats', methods=['GET'])
def server_stats(server_id):
    stats = get_stats(server_id)
    if not stats:
        return jsonify({'error': 'Could not get stats (server may be stopped)'}), 404

    # Parse CPU
    cpu_str = stats.get('CPUPerc', '0%').replace('%', '')
    mem_usage = stats.get('MemUsage', '0B / 0B')
    net_io = stats.get('NetIO', '0B / 0B')
    block_io = stats.get('BlockIO', '0B / 0B')
    
    return jsonify({
        'cpu': cpu_str,
        'memory': mem_usage,
        'pids': stats.get('PIDs', '0'),
    })

@app.route('/api/servers/<server_id>/world-size', methods=['GET'])
def world_size(server_id):
    # Fast path: exec into the running container
    code, out, _ = docker_run(['exec', server_id, 'du', '-sh', '/data'], quiet=True, timeout=15)
    if code == 0 and out:
        return jsonify({'size': out.split('\t')[0].strip()})
    return jsonify({'size': '—'})

@app.route('/api/servers/<server_id>/logs', methods=['GET'])
def server_logs(server_id):
    lines = request.args.get('lines', 100)
    code, out, err = docker_run(['logs', '--tail', str(lines), server_id])
    if code != 0:
        return jsonify({'logs': err or 'No logs available'})
    return jsonify({'logs': out or '(no output yet)'})

@app.route('/api/servers/<server_id>/command', methods=['POST'])
def send_command(server_id):
    data = request.json
    command = data.get('command', '').strip()
    if not command:
        return jsonify({'error': 'Command is required'}), 400

    log.info('Command on %s: %s', server_id, command)
    # Use rcon-cli via docker exec
    code, out, err = docker_run([
        'exec', server_id,
        'rcon-cli', '--host', '127.0.0.1', '--password', 'mcpanel', command
    ])
    
    if code != 0:
        # Fallback: try sending directly to container stdin
        try:
            result = subprocess.run(
                ['docker', 'exec', '-i', server_id, 'sh', '-c',
                 f'mc-send-to-console "{command}"'],
                capture_output=True, text=True, timeout=10
            )
            out = result.stdout
            err = result.stderr
            code = result.returncode
        except Exception as e:
            err = str(e)

    return jsonify({
        'success': code == 0,
        'output': out or err or 'Command sent'
    })

@app.route('/api/servers/<server_id>/players', methods=['GET'])
def get_players(server_id):
    code, out, err = docker_run([
        'exec', server_id,
        'rcon-cli', '--host', '127.0.0.1', '--password', 'mcpanel', 'list'
    ], quiet=True)
    raw = out or ''
    # Parse "There are N of a max M players online: name1, name2"
    players = []
    if ':' in raw:
        names_part = raw.split(':', 1)[1].strip()
        if names_part:
            players = [n.strip() for n in names_part.split(',') if n.strip()]
    return jsonify({'output': raw or err or 'Server may be starting...', 'players': players})

@app.route('/api/servers/<server_id>/whitelist', methods=['GET'])
def get_whitelist(server_id):
    code, out, err = docker_run([
        'exec', server_id,
        'rcon-cli', '--host', '127.0.0.1', '--password', 'mcpanel', 'whitelist list'
    ], quiet=True)
    raw = out or ''
    # Parse "There are N whitelisted player(s): name1, name2"
    players = []
    if ':' in raw:
        names_part = raw.split(':', 1)[1].strip()
        if names_part:
            players = [n.strip() for n in names_part.split(',') if n.strip()]
    return jsonify({'output': raw or err or 'Server may be starting...', 'players': players})

@app.route('/api/servers/<server_id>/whitelist', methods=['POST'])
def update_whitelist(server_id):
    data = request.json or {}
    player = data.get('player', '').strip()
    action = data.get('action', 'add')
    if not player:
        return jsonify({'error': 'Player name is required'}), 400
    if action not in ('add', 'remove'):
        return jsonify({'error': 'action must be "add" or "remove"'}), 400

    code, out, err = docker_run([
        'exec', server_id,
        'rcon-cli', '--host', '127.0.0.1', '--password', 'mcpanel', f'whitelist {action} {player}'
    ])
    if code != 0:
        return jsonify({'error': err or 'Failed to update whitelist'}), 500

    log.info('Whitelist %s %s on %s', action, player, server_id)
    return jsonify({'success': True, 'output': out or err})

def _icon_path(server_name):
    p = os.path.join(ICONS_DIR, secure_filename(server_name) + '.png')
    return p if os.path.exists(p) else None

def _write_server_icon_to_volume(volume_name, png_bytes):
    """Write a 64x64 server-icon.png into the instance's /data so the MC client picks it up."""
    result = subprocess.run([
        'docker', 'run', '--rm', '-i',
        '-v', f'{volume_name}:/data',
        'alpine',
        'sh', '-c', 'cat > /data/server-icon.png'
    ], input=png_bytes, capture_output=True, timeout=60)
    return result.returncode == 0, result.stderr.decode(errors='replace').strip()

def _remove_server_icon_from_volume(volume_name):
    result = subprocess.run([
        'docker', 'run', '--rm',
        '-v', f'{volume_name}:/data',
        'alpine',
        'sh', '-c', 'rm -f /data/server-icon.png'
    ], capture_output=True, text=True, timeout=30)
    return result.returncode == 0

@app.route('/api/servers/<server_id>/icon', methods=['GET'])
def get_server_icon(server_id):
    code, name, _ = docker_run(['inspect', server_id, '--format', '{{.Name}}'])
    server_name = name.lstrip('/') if code == 0 else server_id
    path = _icon_path(server_name)
    if not path:
        return jsonify({'error': 'No icon set'}), 404
    return send_file(path)

@app.route('/api/servers/<server_id>/icon', methods=['POST'])
def upload_server_icon(server_id):
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'No file selected'}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_ICON_EXTS:
        return jsonify({'error': 'Allowed image types: PNG, JPG, GIF, WEBP'}), 400

    data = f.read()
    if len(data) > MAX_ICON_SIZE:
        return jsonify({'error': 'Image must be smaller than 2 MB'}), 400

    try:
        img = Image.open(io.BytesIO(data))
        img = img.convert('RGBA').resize((64, 64), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        icon_png = buf.getvalue()
    except Exception:
        return jsonify({'error': 'Could not read that image file'}), 400

    code, name, _ = docker_run(['inspect', server_id, '--format', '{{.Name}}'])
    server_name = name.lstrip('/') if code == 0 else server_id
    safe_name = secure_filename(server_name)

    with open(os.path.join(ICONS_DIR, safe_name + '.png'), 'wb') as out:
        out.write(icon_png)

    volume_name = _get_volume_name(server_id)
    ok, err = _write_server_icon_to_volume(volume_name, icon_png)
    if not ok:
        log.warning('Failed to write server-icon.png into %s: %s', server_name, err)
        return jsonify({'error': 'Saved panel icon, but failed to apply it to the server: ' + (err or 'unknown error')}), 500

    log.info('Icon uploaded for server %s', server_name)
    return jsonify({'success': True})

@app.route('/api/servers/<server_id>/icon', methods=['DELETE'])
def delete_server_icon(server_id):
    code, name, _ = docker_run(['inspect', server_id, '--format', '{{.Name}}'])
    server_name = name.lstrip('/') if code == 0 else server_id
    path = _icon_path(server_name)
    if path:
        os.remove(path)

    volume_name = _get_volume_name(server_id)
    _remove_server_icon_from_volume(volume_name)

    log.info('Icon removed for server %s', server_name)
    return jsonify({'success': True})

PLUGIN_SERVER_TYPES = {'SPIGOT', 'PAPER', 'PURPUR'}

def _get_volume_name(server_id):
    code, name, _ = docker_run(['inspect', server_id, '--format', '{{.Name}}'])
    server_name = name.lstrip('/') if code == 0 else server_id
    code, vol_out, _ = docker_run(['inspect', server_id, '--format', '{{range .Mounts}}{{.Name}}{{end}}'])
    return vol_out.strip() if code == 0 and vol_out.strip() else f'mc-{server_name}-data'

def _get_addons_dir(server_id):
    """Return 'plugins' for Bukkit-family servers, 'mods' for everything else."""
    code, out, _ = docker_run(
        ['inspect', server_id, '--format', '{{index .Config.Labels "mc-type"}}'], quiet=True
    )
    server_type = out.strip().upper() if code == 0 else ''
    return 'plugins' if server_type in PLUGIN_SERVER_TYPES else 'mods'

def _list_mods(volume_name, addons_dir='mods'):
    result = subprocess.run([
        'docker', 'run', '--rm',
        '-v', f'{volume_name}:/data',
        'alpine',
        'sh', '-c', 'mkdir -p "/data/$1" && for f in "/data/$1"/*; do [ -f "$f" ] && stat -c "%s %n" "$f"; done',
        '--', addons_dir
    ], capture_output=True, text=True, timeout=30)
    mods = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        size, _, path = line.partition(' ')
        if not path:
            continue
        mods.append({'filename': os.path.basename(path), 'size': int(size)})
    return mods

def _add_mod_file(volume_name, file_storage, addons_dir='mods'):
    filename = secure_filename(file_storage.filename)
    if not filename.lower().endswith('.jar'):
        return False, 'Only .jar files are supported'

    # Stream the upload into the container over stdin rather than bind-mounting
    # a tempdir — when MAP itself runs in Docker, host bind-mount paths aren't
    # valid for sibling containers (see _panel_data_volume).
    result = subprocess.run([
        'docker', 'run', '--rm', '-i',
        '-v', f'{volume_name}:/data',
        'alpine',
        'sh', '-c', 'mkdir -p "/data/$1" && cat > "/data/$1/$2"', '--', addons_dir, filename
    ], input=file_storage.read(), capture_output=True, timeout=60)

    if result.returncode != 0:
        return False, result.stderr.decode(errors='replace').strip() or 'Failed to copy mod into instance'
    return True, filename

def _add_mod_url(volume_name, url, addons_dir='mods'):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return False, 'URL must start with http:// or https://'

    filename = secure_filename(os.path.basename(parsed.path))
    if not filename.lower().endswith('.jar'):
        return False, 'URL must point directly to a .jar file'

    result = subprocess.run([
        'docker', 'run', '--rm',
        '-v', f'{volume_name}:/data',
        'alpine',
        'sh', '-c', 'mkdir -p "/data/$1" && wget -q -O "/data/$1/$2" "$3" || rm -f "/data/$1/$2"',
        '--', addons_dir, filename, url
    ], capture_output=True, text=True, timeout=120)

    if result.returncode != 0:
        return False, result.stderr.strip() or 'Failed to download mod'
    return True, filename

def _delete_mod(volume_name, filename, addons_dir='mods'):
    result = subprocess.run([
        'docker', 'run', '--rm',
        '-v', f'{volume_name}:/data',
        'alpine',
        'sh', '-c', 'rm -f "/data/$1/$2"', '--', addons_dir, filename
    ], capture_output=True, text=True, timeout=30)
    return result.returncode == 0, result.stderr.strip()

@app.route('/api/servers/<server_id>/mods', methods=['GET'])
def list_mods(server_id):
    volume_name = _get_volume_name(server_id)
    addons_dir = _get_addons_dir(server_id)
    mods = _list_mods(volume_name, addons_dir)
    return jsonify({'addons_dir': addons_dir, 'mods': mods})

@app.route('/api/servers/<server_id>/mods', methods=['POST'])
def add_mod(server_id):
    volume_name = _get_volume_name(server_id)
    addons_dir = _get_addons_dir(server_id)

    if 'file' in request.files:
        f = request.files['file']
        if not f.filename:
            return jsonify({'error': 'No file selected'}), 400
        ok, result = _add_mod_file(volume_name, f, addons_dir)
    else:
        data = request.json or {}
        url = data.get('url', '').strip()
        if not url:
            return jsonify({'error': 'A .jar file or download URL is required'}), 400
        ok, result = _add_mod_url(volume_name, url, addons_dir)

    if not ok:
        return jsonify({'error': result}), 400

    log.info('Mod added to %s (%s): %s', server_id, addons_dir, result)
    return jsonify({'success': True, 'filename': result})

@app.route('/api/servers/<server_id>/mods/<filename>', methods=['DELETE'])
def delete_mod(server_id, filename):
    if '..' in filename or '/' in filename:
        return jsonify({'error': 'Invalid filename'}), 400

    volume_name = _get_volume_name(server_id)
    addons_dir = _get_addons_dir(server_id)
    ok, err = _delete_mod(volume_name, filename, addons_dir)
    if not ok:
        return jsonify({'error': err or 'Failed to remove mod'}), 500

    log.info('Mod removed from %s (%s): %s', server_id, addons_dir, filename)
    return jsonify({'success': True})

def _read_server_properties(volume_name):
    result = subprocess.run([
        'docker', 'run', '--rm',
        '-v', f'{volume_name}:/data',
        'alpine',
        'sh', '-c', 'cat /data/server.properties 2>/dev/null'
    ], capture_output=True, text=True, timeout=30)
    return result.stdout if result.returncode == 0 else None

def _write_server_properties(volume_name, content):
    result = subprocess.run([
        'docker', 'run', '--rm', '-i',
        '-v', f'{volume_name}:/data',
        'alpine',
        'sh', '-c', 'cat > /data/server.properties'
    ], input=content, capture_output=True, text=True, timeout=30)
    return result.returncode == 0, result.stderr.strip()

@app.route('/api/servers/<server_id>/properties', methods=['GET'])
def get_properties(server_id):
    volume_name = _get_volume_name(server_id)
    raw = _read_server_properties(volume_name)
    if raw is None:
        return jsonify({'error': 'server.properties not found (server may not have started yet)'}), 404

    lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append({'type': 'blank'})
        elif stripped.startswith('#'):
            lines.append({'type': 'comment', 'value': line})
        elif '=' in stripped:
            k, _, v = stripped.partition('=')
            lines.append({'type': 'property', 'key': k.strip(), 'value': v})
        else:
            lines.append({'type': 'comment', 'value': line})

    return jsonify({'lines': lines})

@app.route('/api/servers/<server_id>/properties', methods=['POST'])
def save_properties(server_id):
    data = request.json or {}
    lines = data.get('lines', [])
    if not isinstance(lines, list):
        return jsonify({'error': 'lines must be a list'}), 400

    parts = []
    for item in lines:
        t = item.get('type', '')
        if t == 'blank':
            parts.append('')
        elif t == 'comment':
            parts.append(item.get('value', ''))
        elif t == 'property':
            key = str(item.get('key', ''))
            value = str(item.get('value', ''))
            if '\n' in key or '\n' in value or '=' in key:
                return jsonify({'error': f'Invalid key or value for: {key!r}'}), 400
            parts.append(f'{key}={value}')
        else:
            return jsonify({'error': f'Unknown line type: {t!r}'}), 400

    content = '\n'.join(parts) + '\n'
    volume_name = _get_volume_name(server_id)
    ok, err = _write_server_properties(volume_name, content)
    if not ok:
        return jsonify({'error': err or 'Failed to write server.properties'}), 500

    log.info('server.properties updated for %s', server_id)
    return jsonify({'success': True})

def _panel_data_volume():
    """Return the named Docker volume backing /data in this container, or None.

    When MAP runs inside Docker, BACKUPS_DIR is a container-local path that the
    host Docker daemon cannot bind-mount.  Using the named volume directly lets
    sibling containers (alpine, minecraft) reach the same data.
    """
    code, out, _ = docker_run(
        ['inspect', socket.gethostname(), '--format', '{{json .Mounts}}'],
        quiet=True
    )
    if code != 0 or not out:
        return None
    try:
        for m in json.loads(out):
            if m.get('Type') == 'volume' and m.get('Destination') == '/data':
                return m['Name']
    except (json.JSONDecodeError, KeyError):
        pass
    return None

def _run_backup(server_id, label=None):
    """Run a backup. Returns (True, filename) or (False, error_str)."""
    if label is None:
        label = datetime.now().strftime('%Y%m%d_%H%M%S')

    code, name, _ = docker_run(['inspect', server_id, '--format', '{{.Name}}'])
    server_name = name.lstrip('/') if code == 0 else server_id

    code, vol_out, _ = docker_run(['inspect', server_id, '--format', '{{range .Mounts}}{{.Name}}{{end}}'])
    volume_name = vol_out.strip() if code == 0 and vol_out.strip() else f'mc-{server_name}-data'

    backup_name = f'{server_name}_{label}.tar.gz'
    backup_path = os.path.join(BACKUPS_DIR, backup_name)
    log.info('Creating backup server=%s label=%s file=%s', server_name, label, backup_name)

    rcon = ['exec', server_id, 'rcon-cli', '--host', '127.0.0.1', '--password', 'mcpanel']
    server_running = docker_run(rcon + ['list'], quiet=True)[0] == 0
    if server_running:
        log.info('Flushing world data on %s before backup', server_name)
        docker_run(rcon + ['save-off'], quiet=True)
        docker_run(rcon + ['save-all'], quiet=True)
        time.sleep(3)

    panel_vol = _panel_data_volume()
    if panel_vol:
        vol_args  = ['-v', f'{panel_vol}:/panel-data']
        dest_path = f'/panel-data/backups/{backup_name}'
    else:
        vol_args  = ['-v', f'{BACKUPS_DIR}:/backups']
        dest_path = f'/backups/{backup_name}'

    result = subprocess.run(
        ['docker', 'run', '--rm',
         '-v', f'{volume_name}:/mc-data',
         *vol_args,
         'alpine',
         'tar', '-czf', dest_path, '-C', '/mc-data', '.'],
        capture_output=True, text=True, timeout=120
    )

    if server_running:
        docker_run(rcon + ['save-on'], quiet=True)

    if not os.path.exists(backup_path):
        log.error('Backup docker run rc=%d stdout=%r stderr=%r',
                  result.returncode, result.stdout[:500], result.stderr[:500])
        return False, result.stderr or result.stdout or 'Backup failed'

    log.info('Backup complete file=%s size=%d', backup_name, os.path.getsize(backup_path))
    return True, backup_name

@app.route('/api/servers/<server_id>/backup', methods=['POST'])
def backup_server(server_id):
    data = request.json or {}
    label = data.get('label', '').strip() or None
    ok, result = _run_backup(server_id, label)
    if not ok:
        log.error('Backup failed for server %s: %s', server_id, result)
        return jsonify({'error': 'Backup failed: ' + result}), 500
    return jsonify({
        'success': True,
        'filename': result,
        'size': os.path.getsize(os.path.join(BACKUPS_DIR, result)),
    })

@app.route('/api/servers/<server_id>/backups', methods=['GET'])
def list_backups(server_id):
    code, name, _ = docker_run([
        'inspect', server_id, '--format', '{{.Name}}'
    ])
    server_name = name.lstrip('/') if code == 0 else server_id

    backups = []
    for f in sorted(os.listdir(BACKUPS_DIR), reverse=True):
        if f.startswith(server_name + '_') and f.endswith('.tar.gz'):
            fpath = os.path.join(BACKUPS_DIR, f)
            backups.append({
                'filename': f,
                'size': os.path.getsize(fpath),
                'created': datetime.fromtimestamp(
                    os.path.getmtime(fpath)
                ).strftime('%Y-%m-%d %H:%M:%S')
            })
    return jsonify(backups)

@app.route('/api/backups/<filename>/restore/<server_id>', methods=['POST'])
def restore_backup(filename, server_id):
    backup_path = os.path.join(BACKUPS_DIR, filename)
    if not os.path.exists(backup_path):
        return jsonify({'error': 'Backup file not found'}), 404

    # Sanitize filename
    if '..' in filename or '/' in filename:
        return jsonify({'error': 'Invalid filename'}), 400

    # Get volume
    code, name, _ = docker_run(['inspect', server_id, '--format', '{{.Name}}'])
    server_name = name.lstrip('/') if code == 0 else server_id
    volume_name = f'mc-{server_name}-data'

    log.info('Restoring backup file=%s to server=%s', filename, server_id)
    # Stop server
    docker_run(['stop', server_id])
    time.sleep(2)

    # Restore
    panel_vol = _panel_data_volume()
    if panel_vol:
        vol_args = ['-v', f'{panel_vol}:/panel-data']
        src_path = f'/panel-data/backups/{filename}'
    else:
        vol_args = ['-v', f'{BACKUPS_DIR}:/backups']
        src_path = f'/backups/{filename}'

    result = subprocess.run(
        ['docker', 'run', '--rm',
         '-v', f'{volume_name}:/data',
         *vol_args,
         'alpine',
         'sh', '-c', 'rm -rf /data/* && tar -xzf "$1" -C /data', '--', src_path],
        capture_output=True, text=True, timeout=120
    )

    # Restart server
    docker_run(['start', server_id])

    if result.returncode != 0:
        log.error('Restore failed file=%s server=%s: %s', filename, server_id, result.stderr)
        return jsonify({'error': 'Restore failed: ' + result.stderr}), 500

    log.info('Restore complete file=%s server=%s', filename, server_id)
    return jsonify({'success': True})

@app.route('/api/backups/<filename>/download', methods=['GET'])
def download_backup(filename):
    if '..' in filename or '/' in filename:
        return jsonify({'error': 'Invalid filename'}), 400
    backup_path = os.path.join(BACKUPS_DIR, filename)
    if not os.path.exists(backup_path):
        return jsonify({'error': 'File not found'}), 404
    return send_file(backup_path, as_attachment=True)

@app.route('/api/backups/<filename>', methods=['DELETE'])
def delete_backup(filename):
    if '..' in filename or '/' in filename:
        return jsonify({'error': 'Invalid filename'}), 400
    backup_path = os.path.join(BACKUPS_DIR, filename)
    if os.path.exists(backup_path):
        os.remove(backup_path)
    return jsonify({'success': True})

def _load_schedules():
    try:
        with open(SCHEDULES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_schedules(schedules):
    with open(SCHEDULES_FILE, 'w') as f:
        json.dump(schedules, f, indent=2)

def _run_scheduled_backup(server_name):
    ok, result = _run_backup(server_name)
    if ok:
        log.info('Scheduled backup complete for %s: %s', server_name, result)
    else:
        log.error('Scheduled backup FAILED for %s: %s', server_name, result)

def _scheduler_loop():
    log.info('Backup scheduler started')
    while True:
        time.sleep(60)
        now = time.time()
        with _schedules_lock:
            schedules = _load_schedules()
            changed = False
            for server_name, sched in list(schedules.items()):
                if not sched.get('enabled'):
                    continue
                if now >= sched.get('next_run', 0):
                    log.info('Triggering scheduled backup for %s', server_name)
                    sched['last_run'] = now
                    sched['next_run'] = now + sched.get('interval_hours', 24) * 3600
                    changed = True
                    threading.Thread(target=_run_scheduled_backup, args=(server_name,), daemon=True).start()
            if changed:
                _save_schedules(schedules)

@app.route('/api/servers/<server_id>/schedule', methods=['GET'])
def get_schedule(server_id):
    code, name, _ = docker_run(['inspect', server_id, '--format', '{{.Name}}'])
    server_name = name.lstrip('/') if code == 0 else server_id
    with _schedules_lock:
        schedules = _load_schedules()
    sched = schedules.get(server_name, {'enabled': False, 'interval_hours': 24})
    result = dict(sched)
    if 'next_run' in result:
        result['next_run_fmt'] = datetime.fromtimestamp(result['next_run']).strftime('%Y-%m-%d %H:%M')
    if 'last_run' in result:
        result['last_run_fmt'] = datetime.fromtimestamp(result['last_run']).strftime('%Y-%m-%d %H:%M')
    return jsonify(result)

@app.route('/api/servers/<server_id>/schedule', methods=['POST'])
def set_schedule(server_id):
    data = request.json or {}
    enabled = bool(data.get('enabled', False))
    try:
        interval_hours = int(data.get('interval_hours', 24))
    except (TypeError, ValueError):
        return jsonify({'error': 'interval_hours must be an integer'}), 400
    if interval_hours < 1:
        return jsonify({'error': 'Interval must be at least 1 hour'}), 400

    code, name, _ = docker_run(['inspect', server_id, '--format', '{{.Name}}'])
    server_name = name.lstrip('/') if code == 0 else server_id

    # Set next_run to now so the scheduler fires within the next 60 s on first enable
    next_run = time.time() if enabled else None
    with _schedules_lock:
        schedules = _load_schedules()
        existing = schedules.get(server_name, {})
        schedules[server_name] = {**existing, 'enabled': enabled, 'interval_hours': interval_hours}
        if next_run:
            schedules[server_name]['next_run'] = next_run
        else:
            schedules[server_name].pop('next_run', None)
        _save_schedules(schedules)

    next_run_fmt = 'within 1 minute' if enabled else None
    log.info('Schedule updated for %s: enabled=%s interval=%dh', server_name, enabled, interval_hours)
    return jsonify({'success': True, 'next_run_fmt': next_run_fmt})

@app.route('/api/system', methods=['GET'])
def system_info():
    # Overall Docker info
    code, out, _ = docker_run(['info', '--format', '{{json .}}'])
    info = {}
    if code == 0 and out:
        try:
            d = json.loads(out)
            info = {
                'containers': d.get('Containers', 0),
                'running': d.get('ContainersRunning', 0),
                'docker_version': d.get('ServerVersion', 'unknown'),
            }
        except json.JSONDecodeError:
            pass

    # Disk usage for backups
    backup_size = sum(
        os.path.getsize(os.path.join(BACKUPS_DIR, f))
        for f in os.listdir(BACKUPS_DIR)
        if os.path.isfile(os.path.join(BACKUPS_DIR, f))
    )

    return jsonify({
        **info,
        'backup_count': len(os.listdir(BACKUPS_DIR)),
        'backup_size': backup_size,
    })

_debug = os.environ.get('APP_ENV', 'development') != 'production'
app.debug = _debug

if __name__ == '__main__':
    # WERKZEUG_RUN_MAIN=true → Werkzeug reloader child (actual server).
    # No WERKZEUG_RUN_MAIN   → production / direct run without reloader.
    # Either way, start the scheduler exactly once in the serving process.
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not _debug:
        threading.Thread(target=_scheduler_loop, daemon=True).start()
    app.run(debug=_debug, host='0.0.0.0', port=5000)
elif not _debug:
    # Imported by a WSGI server (e.g. gunicorn) in production: start the
    # scheduler once here since the __main__ guard above won't run.
    threading.Thread(target=_scheduler_loop, daemon=True).start()
