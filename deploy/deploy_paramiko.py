#!/usr/bin/env python3
"""
ECH Deploy via paramiko — works without sshpass, handles password auth.
Run from the project root:
    python deploy/deploy_paramiko.py
Reads credentials from deploy/local.env (gitignored).
"""

import io, os, sys, tarfile, time

def load_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                k, _, v = line.partition('=')
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env

def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env  = load_env(os.path.join(root, 'deploy', 'local.env'))

    HOST      = env.get('ECH_SSH_HOST', '192.168.6.200')
    USER      = env.get('ECH_SSH_USER', 'mesh')
    PASS      = env.get('ECH_SSH_PASS', '')
    SUDO_PASS = env.get('ECH_SUDO_PASS', PASS)

    version = open(os.path.join(root, 'VERSION')).read().strip()
    print(f"=== ECH Deploy - v{version} ===")

    # ── Build tarball in memory ────────────────────────────────────────────
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for base in ('ech',):
            for dirpath, dirs, files in os.walk(os.path.join(root, base)):
                dirs[:] = [d for d in dirs if d not in ('__pycache__', '.git', 'node_modules')]
                for fname in files:
                    fpath = os.path.join(dirpath, fname)
                    arcname = os.path.relpath(fpath, root)
                    tar.add(fpath, arcname=arcname)
        for extra in ('config.yaml', 'config-sim.yaml', 'VERSION', 'deploy/install.sh', 'deploy/ech-sim.service'):
            fpath = os.path.join(root, extra)
            if os.path.exists(fpath):
                tar.add(fpath, arcname=extra)
    buf.seek(0)
    print(f"Tarball: {len(buf.getvalue())/1024:.1f} KB")

    try:
        import paramiko
    except ImportError:
        print("ERROR: paramiko not installed. Run: pip install paramiko")
        sys.exit(1)

    # ── Connect ────────────────────────────────────────────────────────────
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASS, timeout=15)
    print(f"Connected to {USER}@{HOST}")

    # ── Upload via SFTP ────────────────────────────────────────────────────
    sftp = ssh.open_sftp()
    buf.seek(0)
    sftp.putfo(buf, '/tmp/ech_deploy.tar.gz')
    with open(os.path.join(root, 'deploy', 'install.sh'), 'rb') as f:
        sftp.putfo(f, '/tmp/install.sh')
    sftp.close()
    print("Uploaded tarball + install.sh")

    # ── Run install.sh ─────────────────────────────────────────────────────
    print("Running install.sh ...")
    chan = ssh.get_transport().open_session()
    chan.get_pty()
    chan.exec_command(f'echo "{SUDO_PASS}" | sudo -S bash /tmp/install.sh')

    while True:
        if chan.recv_ready():
            print(chan.recv(4096).decode('utf-8', errors='replace'), end='')
        if chan.recv_stderr_ready():
            print(chan.recv_stderr(4096).decode('utf-8', errors='replace'), end='', file=sys.stderr)
        if chan.exit_status_ready():
            break
        time.sleep(0.1)

    exit_code = chan.recv_exit_status()
    ssh.close()
    status = 'complete' if exit_code == 0 else f'FAILED (exit {exit_code})'
    print(f"\n=== Deploy {status} - v{version} ===")
    sys.exit(0 if exit_code == 0 else 1)

if __name__ == '__main__':
    main()
