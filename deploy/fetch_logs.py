#!/usr/bin/env python3
"""Fetch ECH service logs from the live server."""
import io, os, sys, time

def load_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'): continue
                k, _, v = line.partition('=')
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env

def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env  = load_env(os.path.join(root, 'deploy', 'local.env'))
    HOST = env.get('ECH_SSH_HOST', '192.168.6.200')
    USER = env.get('ECH_SSH_USER', 'mesh')
    PASS = env.get('ECH_SSH_PASS', '')
    SUDO = env.get('ECH_SUDO_PASS', PASS)
    since = sys.argv[1] if len(sys.argv) > 1 else '10 minutes ago'
    lines = sys.argv[2] if len(sys.argv) > 2 else '200'

    try:
        import paramiko
    except ImportError:
        print("ERROR: pip install paramiko")
        sys.exit(1)

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASS, timeout=15)

    cmd = f'echo "{SUDO}" | sudo -S journalctl -u ech --no-pager -n {lines} --since "{since}"'
    chan = ssh.get_transport().open_session()
    chan.get_pty()
    chan.exec_command(cmd)

    out = b''
    while True:
        if chan.recv_ready():   out += chan.recv(16384)
        if chan.exit_status_ready(): break
        time.sleep(0.05)
    if chan.recv_ready(): out += chan.recv(16384)
    ssh.close()

    text = out.decode('utf-8', errors='replace')
    # strip sudo password echo line
    lines_out = [l for l in text.splitlines() if SUDO not in l]
    sys.stdout.buffer.write(('\n'.join(lines_out) + '\n').encode('utf-8'))

if __name__ == '__main__':
    main()
