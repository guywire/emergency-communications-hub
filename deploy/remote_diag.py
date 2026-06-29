#!/usr/bin/env python3
"""Run a diagnostic command on the live server and print output."""
import os, sys, time

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

def run(cmd, sudo_pass=''):
    import paramiko
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env  = load_env(os.path.join(root, 'deploy', 'local.env'))
    HOST = env.get('ECH_SSH_HOST', '192.168.6.200')
    USER = env.get('ECH_SSH_USER', 'mesh')
    PASS = env.get('ECH_SSH_PASS', '')
    SUDO = env.get('ECH_SUDO_PASS', PASS)

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASS, timeout=15)

    full_cmd = f'echo "{SUDO}" | sudo -S bash -c {repr(cmd)}'
    chan = ssh.get_transport().open_session()
    chan.get_pty()
    chan.exec_command(full_cmd)

    out = b''
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if chan.recv_ready():   out += chan.recv(16384)
        if chan.exit_status_ready(): break
        time.sleep(0.05)
    if chan.recv_ready(): out += chan.recv(16384)
    ssh.close()

    text = out.decode('utf-8', errors='replace')
    lines = [l for l in text.splitlines() if SUDO not in l]
    return '\n'.join(lines)

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'echo ok'
    sys.stdout.buffer.write((run(cmd) + '\n').encode('utf-8', errors='replace'))
