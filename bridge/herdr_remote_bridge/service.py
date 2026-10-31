"""Per-user autostart registration; renderers are pure and independently testable."""
import getpass
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

LABEL = 'app.herdr-remote.bridge'
UNIT = 'herdr-remote-bridge.service'
TASK = 'Herdr Remote Bridge'


def render_plist(executable, config, path, home):
    log = str(Path(home) / 'Library/Logs/herdr-remote-bridge.log')
    return plistlib.dumps(dict(Label=LABEL, ProgramArguments=[executable, 'serve'],
        EnvironmentVariables={'HERD_CONFIG': config, 'PATH': path}, RunAtLoad=True,
        KeepAlive=True, ThrottleInterval=10, StandardOutPath=log, StandardErrorPath=log)).decode()


def unit_quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('\n', '\\n').replace('\r', '\\r') + '"'


def render_unit(executable, config, path):
    return ('[Unit]\nDescription=Herdr Remote bridge\nAfter=network-online.target\n\n[Service]\n'
            f'Environment={unit_quote("HERD_CONFIG=" + config)}\nEnvironment={unit_quote("PATH=" + path)}\n'
            f'ExecStart={unit_quote(executable)} serve\nRestart=always\nRestartSec=10\n\n'
            '[Install]\nWantedBy=default.target\n')


def render_task(executable, config, path, user):
    # Task Scheduler has no action environment: use a private Python wrapper,
    # with config/PATH embedded there, rather than putting secrets in task XML.
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger></Triggers>
<Principals><Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Hidden>true</Hidden><Enabled>true</Enabled><RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure></Settings>
<Actions Context="Author"><Exec><Command>{escape(executable)}</Command><Arguments>{escape(config)}</Arguments><WorkingDirectory>{escape(path)}</WorkingDirectory></Exec></Actions>
</Task>'''


def manage(action):
    from .server import config_path
    system = platform.system()
    home = Path.home()
    executable = shutil.which('herdr-remote-bridge')
    if action == 'install' and not executable:
        raise ValueError('Install the bridge with pipx and add it to PATH first.')
    config = str(config_path().resolve())
    path = os.environ.get('PATH', '')
    def run(args, check=True):
        return subprocess.run(args, check=check).returncode
    if system == 'Darwin':
        target = home / 'Library/LaunchAgents' / (LABEL + '.plist')
        domain = f'gui/{os.getuid()}'
        if action == 'status':
            return run(['launchctl', 'print', domain + '/' + LABEL], False)
        run(['launchctl', 'bootout', domain + '/' + LABEL], False)
        if action == 'install':
            target.parent.mkdir(parents=True, exist_ok=True)
            (home / 'Library/Logs').mkdir(parents=True, exist_ok=True)
            target.write_text(render_plist(executable, config, path, home), encoding='utf-8')
            run(['launchctl', 'bootstrap', domain, str(target)])
        else:
            target.unlink(missing_ok=True)
    elif system == 'Linux':
        target = home / '.config/systemd/user' / UNIT
        if action == 'status':
            return run(['systemctl', '--user', 'status', UNIT], False)
        if action == 'install':
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render_unit(executable, config, path), encoding='utf-8')
            run(['systemctl', '--user', 'daemon-reload'])
            run(['systemctl', '--user', 'enable', '--now', UNIT])
            run(['systemctl', '--user', 'restart', UNIT])
            print('For startup without login, run (may require authorization): loginctl enable-linger ' + getpass.getuser())
        else:
            run(['systemctl', '--user', 'disable', '--now', UNIT], False)
            target.unlink(missing_ok=True)
            run(['systemctl', '--user', 'daemon-reload'])
    elif system == 'Windows':
        prefix = ['$ErrorActionPreference="Stop";']
        if action == 'status':
            script = "Get-ScheduledTask -TaskName 'Herdr Remote Bridge' | Get-ScheduledTaskInfo"
        elif action == 'uninstall':
            script = "Get-ScheduledTask -TaskName 'Herdr Remote Bridge' -ErrorAction SilentlyContinue | Stop-ScheduledTask; Get-ScheduledTask -TaskName 'Herdr Remote Bridge' -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false"
        else:
            # pythonw hides the console; the wrapper preserves the paired config and PATH.
            wrapper = Path(config).parent / 'service_runner.py'
            wrapper.parent.mkdir(parents=True, exist_ok=True)
            wrapper.write_text('import os, subprocess\n' + f'os.environ["HERD_CONFIG"] = {config!r}\nos.environ["PATH"] = {path!r}\n' +
                               f'raise SystemExit(subprocess.call([{executable!r}, "serve"], creationflags=0x08000000))\n', encoding='utf-8')
            pythonw = str(Path(sys.executable).with_name('pythonw.exe'))
            user = subprocess.check_output(['whoami'], text=True).strip()
            xml = render_task(pythonw, subprocess.list2cmdline([str(wrapper)]), str(wrapper.parent), user)
            with tempfile.NamedTemporaryFile(suffix='.xml', delete=False) as stream:
                xmlpath = Path(stream.name)
                stream.write(xml.encode('utf-16'))
            try:
                quoted = str(xmlpath).replace("'", "''")
                run(['powershell.exe', '-NoProfile', '-Command', prefix[0] +
                     f"Get-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue | Stop-ScheduledTask; Register-ScheduledTask -TaskName '{TASK}' -Xml (Get-Content -Raw -LiteralPath '{quoted}') -Force | Out-Null; Start-ScheduledTask -TaskName '{TASK}'"])
            finally:
                xmlpath.unlink(missing_ok=True)
            return 0
        run(['powershell.exe', '-NoProfile', '-Command', prefix[0] + script])
        if action == 'uninstall':
            (Path(config).parent / 'service_runner.py').unlink(missing_ok=True)
    else:
        raise ValueError('Service registration supports macOS, Linux and Windows.')
    return 0
