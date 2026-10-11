#!/usr/bin/env python3
"""Protected operator lane: subscription-only Astra and bounded micro-tasks."""
import argparse
import os
import subprocess
import sys
from pathlib import Path

HOME = Path('/var/lib/ai-orchestra/codex-worker')


def micro_timeout(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--role', choices=('architect', 'developer', 'qa', 'reviewer'), default='developer')
    parser.add_argument('--timeout', type=int)
    args, _ = parser.parse_known_args(argv)
    seconds = args.timeout if args.timeout is not None else (420 if args.role == 'developer' else 300)
    if not 60 <= seconds <= 600:
        raise ValueError('Micro-task timeout must be 60..600 seconds; split larger tasks')
    return seconds


def main():
    if os.geteuid() == 0:
        return subprocess.run(['runuser', '-u', 'codex-worker', '--', '/usr/local/bin/orchestra-task', *sys.argv[1:]]).returncode
    if Path.home() != HOME:
        raise ValueError('Run as codex-worker or root')
    timeout = micro_timeout(sys.argv[1:])
    os.chdir(HOME)
    args = [sys.executable, str(HOME / 'repos/ai_orchestra/scripts/codex_task.py'), *sys.argv[1:],
            '--model', 'gpt-6-astra', '--codex', '/usr/local/libexec/ai-orchestra/codex-subscription',
            '--worktrees', str(HOME / 'worktrees'), '--output', str(HOME / 'results'),
            '--timeout', str(timeout)]
    return subprocess.run(args).returncode


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
