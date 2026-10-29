"""Per-installation OS lock for lifecycle and configuration mutations."""
from contextlib import contextmanager
import os
from . import config
from .console import Error


@contextmanager
def lock():
    path=config.home()/'operation.lock'
    if path.is_symlink():raise Error('Symlinked operation lock is refused.')
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with open(path,'a+b') as file:
        os.chmod(path,0o600)
        if file.tell()==0:file.write(b'0');file.flush()
        file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:raise Error('Another LVCT operation is active for this installation. Wait for it to finish.') from exc
        try:yield
        finally:
            if os.name=='nt':
                file.seek(0)
                msvcrt.locking(file.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(file,fcntl.LOCK_UN)
