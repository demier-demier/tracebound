"""Build the same Rust analysis core for the browser; no JavaScript substitute solver."""
import os
from pathlib import Path
import shutil
import subprocess

# Compiler panic/source strings can contain the builder's private filesystem path.
# Remap build paths before emitting an artifact intended for public distribution.
env = dict(os.environ)
flags = env.get('RUSTFLAGS', '')
flags += f' --remap-path-prefix={Path.home()}=/build'
flags += f' --remap-path-prefix={Path.cwd()}=/tracebound'
env['RUSTFLAGS'] = flags.strip()
subprocess.run([
    'cargo', 'build', '--release', '--locked', '--lib', '--features', 'browser',
    '--target', 'wasm32-unknown-unknown',
], env=env, check=True)
subprocess.run([
    'wasm-bindgen', 'target/wasm32-unknown-unknown/release/tracebound.wasm',
    '--target', 'web', '--out-dir', 'web/pkg', '--out-name', 'tracebound',
], check=True)
destination = Path('web/cases')
destination.mkdir(exist_ok=True)
for case in Path('examples').glob('*.json'):
    shutil.copyfile(case, destination / case.name)
for artifact in Path('web/pkg').iterdir():
    if artifact.is_file() and os.fsencode(str(Path.home())) in artifact.read_bytes():
        raise RuntimeError('Generated browser artifact still contains a private build path')
print('Built browser solver with remapped source paths and copied original synthetic cases')
