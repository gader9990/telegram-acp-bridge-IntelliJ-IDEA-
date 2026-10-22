"""Build against the installed IDE and AI Assistant; no downloads or Gradle changes."""
import argparse
from pathlib import Path
import shutil
import subprocess
import zipfile

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--idea', type=Path, required=True)
parser.add_argument('--ai-plugin', type=Path, required=True)
args = parser.parse_args()
javac = args.idea / 'jbr/bin/javac.exe'
if not javac.exists():
    javac = shutil.which('javac')
if not javac:
    raise SystemExit('JDK with javac is required')
jars = list((args.idea / 'lib').rglob('*.jar')) + list((args.ai_plugin / 'lib').rglob('*.jar'))
classes = root / 'build/classes'
classes.mkdir(parents=True, exist_ok=True)
arguments = ['--release', '21', '-encoding', 'UTF-8', '-classpath', ';'.join(map(str, jars)),
             '-d', str(classes), str(root / 'src/TelegramChatHelper.java')]
argfile = root / 'build/javac.args'
argfile.write_text('\n'.join('"' + a.replace('\\', '/') + '"' for a in arguments), encoding='utf-8')
subprocess.run([str(javac), '@' + str(argfile)], check=True)
dist = root / 'dist'
dist.mkdir(exist_ok=True)
jar = dist / 'telegram-chat-helper.jar'
with zipfile.ZipFile(jar, 'w', zipfile.ZIP_DEFLATED) as archive:
    archive.write(root / 'plugin.xml', 'META-INF/plugin.xml')
    for path in classes.rglob('*.class'):
        archive.write(path, path.relative_to(classes).as_posix())
with zipfile.ZipFile(dist / 'telegram-chat-helper.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
    archive.write(jar, 'telegram-chat-helper/lib/' + jar.name)
print(dist / 'telegram-chat-helper.zip')
