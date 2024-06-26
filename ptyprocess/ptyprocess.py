import codecs
import errno
import fcntl
import functools
import io
import os
import pickle
import resource
import shutil
import signal
import struct
import sys
import termios
import time

# Constants
from pty import CHILD, STDIN_FILENO
from typing import Any, Callable, Mapping


class PtyProcessError(Exception):
    """Generic error class for this package."""


_platform = sys.platform.lower()

# Solaris uses internal __fork_pty(). All others use pty.fork().
_is_solaris = _platform.startswith("solaris") or _platform.startswith("sunos")

if _is_solaris:
    from ._fork_pty import pty_fork
else:
    from pty import fork as pty_fork


def _byte(i: int) -> bytes:
    return bytes([i])


@functools.cache
def _get_intr_eof() -> tuple[bytes, bytes]:
    """Return the interrupt and EOF characters for the controlling terminal."""

    # inherit EOF and INTR definitions from controlling process.
    try:
        from termios import VEOF, VINTR

        fd = None
        for name in "stdin", "stdout":
            stream = getattr(sys, "__%s__" % name, None)
            if stream is None or not hasattr(stream, "fileno"):
                continue
            try:
                fd = stream.fileno()
            except ValueError:
                continue
        if fd is None:
            # no fd, raise ValueError to fallback on CEOF, CINTR
            raise ValueError("No stream has a fileno")
        intr = ord(termios.tcgetattr(fd)[6][VINTR])
        eof = ord(termios.tcgetattr(fd)[6][VEOF])
    except (ImportError, OSError, IOError, ValueError, termios.error):
        # unless the controlling process is also not a terminal,
        # such as cron(1), or when stdin and stdout are both closed.
        # Fall-back to using CEOF and CINTR. There
        try:
            from termios import CEOF, CINTR

            (intr, eof) = (CINTR, CEOF)
        except ImportError:
            #             ^C, ^D
            (intr, eof) = (3, 4)

    return _byte(intr), _byte(eof)


# setecho and setwinsize are pulled out here because on some platforms, we need
# to do this from the child before we exec()


def _setecho(fd: int, state: bool) -> None:
    errmsg = "setecho() may not be called on this platform (it may still be possible to enable/disable echo when spawning the child process)"

    try:
        attr = termios.tcgetattr(fd)
    except termios.error as err:
        if err.args[0] == errno.EINVAL:
            raise IOError(err.args[0], "%s: %s." % (err.args[1], errmsg))
        raise

    if state:
        attr[3] = attr[3] | termios.ECHO
    else:
        attr[3] = attr[3] & ~termios.ECHO

    try:
        # I tried TCSADRAIN and TCSAFLUSH, but these were inconsistent and
        # blocked on some platforms. TCSADRAIN would probably be ideal.
        termios.tcsetattr(fd, termios.TCSANOW, attr)
    except IOError as err:
        if err.args[0] == errno.EINVAL:
            raise IOError(err.args[0], "%s: %s." % (err.args[1], errmsg))
        raise


def _setwinsize(fd: int, rows: int, cols: int) -> None:
    # Some very old platforms have a bug that causes the value for
    # termios.TIOCSWINSZ to be truncated. There was a hack here to work
    # around this, but it caused problems with newer platforms so has been
    # removed. For details see https://github.com/pexpect/pexpect/issues/39
    TIOCSWINSZ = getattr(termios, "TIOCSWINSZ", -2146929561)
    # Note, assume ws_xpixel and ws_ypixel are zero.
    s = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, TIOCSWINSZ, s)


class PtyProcess:
    """This class represents a process running in a pseudoterminal.

    The main constructor is the :meth:`spawn` classmethod.
    """

    def __init__(
        self,
        pid: int,
        fd: int,
        argv: list | None = None,
        env: Mapping | None = None,
        cwd: str | None = None,
    ):
        self.pid = pid
        self.fd = fd
        self.argv = argv
        self.env = env
        self.cwd = cwd
        readf = io.open(fd, "rb", buffering=0)
        writef = io.open(fd, "wb", buffering=0, closefd=False)
        self.fileobj = io.BufferedRWPair(readf, writef)

        self.terminated = False
        self.closed = False
        self.exitstatus = None
        self.signalstatus = None
        # status returned by os.waitpid
        self.status = None
        self.flag_eof = False
        # Used by close() to give kernel time to update process status.
        # Time in seconds.
        self.delayafterclose = 0.1
        # Used by terminate() to give kernel time to update process status.
        # Time in seconds.
        self.delayafterterminate = 0.1

    @classmethod
    def spawn(
        cls,
        argv: list,
        cwd: str | None = None,
        env: Mapping | None = None,
        echo: bool = True,
        preexec_fn: Callable | None = None,
        dimensions: tuple[int, int] = (24, 80),
        pass_fds: tuple[int, ...] = (),
    ) -> object:
        """Start the given command in a child process in a pseudo terminal.

        This does all the fork/exec type of stuff for a pty, and returns an
        instance of PtyProcess.

        If preexec_fn is supplied, it will be called with no arguments in the
        child process before exec-ing the specified command.
        It may, for instance, set signal handlers to SIG_DFL or SIG_IGN.

        Dimensions of the psuedoterminal used for the subprocess can be
        specified as a tuple (rows, cols), or the default (24, 80) will be used.

        By default, all file descriptors except 0, 1 and 2 are closed. This
        behavior can be overridden with pass_fds, a list of file descriptors to
        keep open between the parent and the child.
        """
        # Note that it is difficult for this method to fail.
        # You cannot detect if the child process cannot start.
        # So the only way you can tell if the child process started
        # or not is to try to read from the file descriptor. If you get
        # EOF immediately then it means that the child is already dead.
        # That may not necessarily be bad because you may have spawned a child
        # that performs some task; creates no stdout output; and then dies.

        if not isinstance(argv, (list, tuple)):
            raise TypeError("Expected a list or tuple for argv, got %r" % argv)

        # Shallow copy of argv so we can modify it
        argv = list(argv[:])
        command = argv[0]

        command_with_path = shutil.which(command)
        if command_with_path is None:
            raise FileNotFoundError(
                "The command was not found or was not " + "executable: %s." % command
            )
        command = command_with_path
        argv[0] = command

        # [issue #119] To prevent the case where exec fails and the user is
        # stuck interacting with a python child process instead of whatever
        # was expected, we implement the solution from
        # http://stackoverflow.com/a/3703179 to pass the exception to the
        # parent process

        # [issue #119] 1. Before forking, open a pipe in the parent process.
        exec_err_pipe_read, exec_err_pipe_write = os.pipe()

        pid, fd = pty_fork()

        # Some platforms must call setwinsize() and setecho() from the
        # child process, and others from the master process. We do both,
        # allowing IOError for either.

        if pid == CHILD:
            # set window size
            try:
                _setwinsize(STDIN_FILENO, *dimensions)
            except IOError as err:
                if err.args[0] not in (errno.EINVAL, errno.ENOTTY):
                    raise

            # disable echo if spawn argument echo was unset
            if not echo:
                try:
                    _setecho(STDIN_FILENO, False)
                except (IOError, termios.error) as err:
                    if err.args[0] not in (errno.EINVAL, errno.ENOTTY):
                        raise

            # [issue #119] 3. The child closes the reading end and sets the
            # close-on-exec flag for the writing end.
            os.close(exec_err_pipe_read)
            fcntl.fcntl(exec_err_pipe_write, fcntl.F_SETFD, fcntl.FD_CLOEXEC)

            # Do not allow child to inherit open file descriptors from parent,
            # with the exception of the exec_err_pipe_write of the pipe
            # and pass_fds.
            # Impose ceiling on max_fd: AIX bugfix for users with unlimited
            # nofiles where resource.RLIMIT_NOFILE is 2^63-1 and os.closerange()
            # occasionally raises out of range error
            max_fd = min(1048576, resource.getrlimit(resource.RLIMIT_NOFILE)[0])
            spass_fds = sorted(set(pass_fds) | {exec_err_pipe_write})
            for pair in zip([2] + spass_fds, spass_fds + [max_fd]):
                os.closerange(pair[0] + 1, pair[1])

            if cwd is not None:
                os.chdir(cwd)

            if preexec_fn is not None:
                try:
                    preexec_fn()
                except Exception as err:
                    with os.fdopen(exec_err_pipe_write, "wb") as f:
                        pickle.dump(err, f)
                    os._exit(1)

            try:
                if env is None:
                    os.execv(command, argv)
                else:
                    os.execvpe(command, argv, env)
            except OSError as err:
                # [issue #119] 5. If exec fails, the child writes the error
                # code back to the parent using the pipe, then exits.
                with os.fdopen(exec_err_pipe_write, "wb") as f:
                    pickle.dump(err, f)
                os._exit(os.EX_OSERR)

        # Parent
        inst = cls(pid, fd, argv, env, cwd)

        # [issue #119] 2. After forking, the parent closes the writing end
        # of the pipe.
        os.close(exec_err_pipe_write)

        # [issue #119] 6. The parent raises EOFError if the
        # child successfully performed exec, since close-on-exec made
        # successful exec close the writing end of the pipe. Or, if exec
        # failed, the parent reads the error code and can proceed
        # accordingly. Either way, the parent blocks until the child calls
        # exec.
        try:
            with os.fdopen(exec_err_pipe_read, "rb") as f:
                err = pickle.load(f)
            raise err
        except EOFError:
            # The parent exited without writing anything, so no bad news.
            pass

        try:
            inst.setwinsize(*dimensions)
        except IOError as err:
            if err.args[0] not in (errno.EINVAL, errno.ENOTTY, errno.ENXIO):
                raise

        return inst

    def __repr__(self) -> str:
        clsname = type(self).__name__
        if self.argv is not None:
            args = [repr(self.argv)]
            if self.env is not None:
                args.append("env=%r" % self.env)
            if self.cwd is not None:
                args.append("cwd=%r" % self.cwd)

            return "{}.spawn({})".format(clsname, ", ".join(args))

        else:
            return "{}(pid={}, fd={})".format(clsname, self.pid, self.fd)

    def __del__(self) -> None:
        """This makes sure that no system resources are left open. Python only
        garbage collects Python objects. OS file descriptors are not Python
        objects, so they must be handled explicitly. If the child file
        descriptor was opened outside of this class (passed to the constructor)
        then this does not close it."""

        if not self.closed:
            # It is possible for __del__ methods to execute during the
            # teardown of the Python VM itself. Thus self.close() may
            # trigger an exception because os.close may be None.
            try:
                self.close()
            # which exception, shouldn't we catch explicitly .. ?
            except Exception:
                pass

    def fileno(self) -> int:
        """This returns the file descriptor of the pty for the child."""
        return self.fd

    def close(self, force: bool = True) -> None:
        """This closes the connection with the child application. Note that
        calling close() more than once is valid. This emulates standard Python
        behavior with files. Set force to True if you want to make sure that
        the child is terminated (SIGKILL is sent if the child ignores SIGHUP
        and SIGINT)."""
        if not self.closed:
            self.flush()
            self.fileobj.close()  # Closes the file descriptor
            # Give kernel time to update process status.
            time.sleep(self.delayafterclose)
            if self.isalive():
                if not self.terminate(force):
                    raise PtyProcessError("Could not terminate the child.")
            self.fd = -1
            self.closed = True
            # self.pid = None

    def flush(self) -> None:
        """This does nothing. It is here to support the interface for a
        File-like object."""

        pass

    def isatty(self) -> bool:
        """This returns True if the file descriptor is open and connected to a
        tty(-like) device, else False.

        On SVR4-style platforms implementing streams, such as SunOS and HP-UX,
        the child pty may not appear as a terminal device.  This means
        methods such as setecho(), setwinsize(), getwinsize() may raise an
        IOError."""

        return os.isatty(self.fd)

    def waitnoecho(self, timeout: int | float | None = None) -> bool:
        """Wait until the terminal ECHO flag is set False.

        This returns True if the echo mode is off, or False if echo was not
        disabled before the timeout. This can be used to detect when the
        child is waiting for a password. Usually a child application will turn
        off echo mode when it is waiting for the user to enter a password. For
        example, instead of expecting the "password:" prompt you can wait for
        the child to turn echo off::

            p = pexpect.spawn('ssh user@example.com')
            p.waitnoecho()
            p.sendline(mypassword)

        If ``timeout=None`` then this method to block until ECHO flag is False.
        """

        if timeout is not None:
            end_time = time.time() + timeout
            while True:
                if not self.getecho():
                    return True
                if timeout < 0:
                    return False
                timeout = end_time - time.time()
                time.sleep(0.1)
        else:
            while True:
                if not self.getecho():
                    return True
                time.sleep(0.1)

    def getecho(self) -> bool:
        """Returns True if terminal echo is on, or False if echo is off.

        Child applications that are expecting you to enter a password often
        disable echo. See also :meth:`waitnoecho`.

        Not supported on platforms where ``isatty()`` returns False.
        """

        try:
            attr = termios.tcgetattr(self.fd)
        except termios.error as err:
            errmsg = "getecho() may not be called on this platform"
            if err.args[0] == errno.EINVAL:
                raise IOError(err.args[0], "%s: %s." % (err.args[1], errmsg))
            raise

        self.echo = bool(attr[3] & termios.ECHO)
        return self.echo

    def setecho(self, state: bool) -> None:
        """Enable or disable terminal echo.

        Anything the child sent before the echo will be lost, so you should be
        sure that your input buffer is empty before you call setecho().
        For example, the following will work as expected::

            p = pexpect.spawn('cat') # Echo is on by default.
            p.sendline('1234') # We expect see this twice from the child...
            p.expect(['1234']) # ... once from the tty echo...
            p.expect(['1234']) # ... and again from cat itself.
            p.setecho(False) # Turn off tty echo
            p.sendline('abcd') # We will set this only once (echoed by cat).
            p.sendline('wxyz') # We will set this only once (echoed by cat)
            p.expect(['abcd'])
            p.expect(['wxyz'])

        The following WILL NOT WORK because the lines sent before the setecho
        will be lost::

            p = pexpect.spawn('cat')
            p.sendline('1234')
            p.setecho(False) # Turn off tty echo
            p.sendline('abcd') # We will set this only once (echoed by cat).
            p.sendline('wxyz') # We will set this only once (echoed by cat)
            p.expect(['1234'])
            p.expect(['1234'])
            p.expect(['abcd'])
            p.expect(['wxyz'])


        Not supported on platforms where ``isatty()`` returns False.
        """
        _setecho(self.fd, state)

        self.echo = state

    def read(self, size: int = 1024) -> str | bytes:
        """Read and return at most ``size`` bytes from the pty.

        Can block if there is nothing to read. Raises :exc:`EOFError` if the
        terminal was closed.

        Unlike Pexpect's ``read_nonblocking`` method, this doesn't try to deal
        with the vagaries of EOF on platforms that do strange things, like IRIX
        or older Solaris systems. It handles the errno=EIO pattern used on
        Linux, and the empty-string return used on BSD platforms and (seemingly)
        on recent Solaris.
        """
        try:
            s = self.fileobj.read1(size)
        except (OSError, IOError) as err:
            if err.args[0] == errno.EIO:
                # Linux-style EOF
                self.flag_eof = True
                raise EOFError("End Of File (EOF). Exception style platform.")
            raise
        if s == b"":
            # BSD-style EOF (also appears to work on recent Solaris (OpenIndiana))
            self.flag_eof = True
            raise EOFError("End Of File (EOF). Empty string style platform.")

        return s

    def readline(self) -> str | bytes:
        """Read one line from the pseudoterminal, and return it as unicode.

        Can block if there is nothing to read. Raises :exc:`EOFError` if the
        terminal was closed.
        """
        try:
            s = self.fileobj.readline()
        except (OSError, IOError) as err:
            if err.args[0] == errno.EIO:
                # Linux-style EOF
                self.flag_eof = True
                raise EOFError("End Of File (EOF). Exception style platform.")
            raise
        if s == b"":
            # BSD-style EOF (also appears to work on recent Solaris (OpenIndiana))
            self.flag_eof = True
            raise EOFError("End Of File (EOF). Empty string style platform.")

        return s

    def _writeb(self, b: Any, flush: bool = True) -> int:
        # 'b' is defined as 'Any'. I wanted 'str | bytes' but my linter complains that
        # those are incompatible with ReadableBuffer, which is what fileobj.write()
        # apparently wants. And that doesn't seem to be a standard type.
        n = self.fileobj.write(b)
        if flush:
            self.fileobj.flush()
        return n

    def write(self, s: str | bytes, flush=True) -> int:
        """Write bytes to the pseudoterminal.

        Returns the number of bytes written.
        """
        return self._writeb(s, flush=flush)

    def sendcontrol(self, char: str) -> tuple[int, bytes]:
        """Helper method for sending control characters to the terminal.

        For example, to send Ctrl-G (ASCII 7, bell, ``'\\a'``)::

            child.sendcontrol('g')

        See also, :meth:`sendintr` and :meth:`sendeof`.
        """
        char = char.lower()
        a = ord(char)
        if 97 <= a <= 122:
            a = a - ord("a") + 1
            byte = _byte(a)
            return self._writeb(byte), byte
        d = {
            "@": 0,
            "`": 0,
            "[": 27,
            "{": 27,
            "\\": 28,
            "|": 28,
            "]": 29,
            "}": 29,
            "^": 30,
            "~": 30,
            "_": 31,
            "?": 127,
        }
        if char not in d:
            return 0, b""

        byte = _byte(d[char])
        return self._writeb(byte), byte

    def sendeof(self) -> tuple[int, bytes | None]:
        """Sends an EOF (typically Ctrl-D) through the terminal.

        This sends a character which causes
        the pending parent output buffer to be sent to the waiting child
        program without waiting for end-of-line. If it is the first character
        of the line, the read() in the user program returns 0, which signifies
        end-of-file. This means to work as expected a sendeof() has to be
        called at the beginning of a line. This method does not send a newline.
        It is the responsibility of the caller to ensure the eof is sent at the
        beginning of a line.
        """
        eof = _get_intr_eof()[1]
        return self._writeb(eof), eof

    def sendintr(self) -> tuple[int, bytes | None]:
        """Send an interrupt character (typically Ctrl-C) through the terminal.

        This will normally trigger the kernel to send SIGINT to the current
        foreground process group. Processes can turn off this translation, in
        which case they can read the raw data sent, e.g. ``b'\\x03'`` for Ctrl-C.

        See also the :meth:`kill` method, which sends a signal directly to the
        immediate child process in the terminal (which is not necessarily the
        foreground process).
        """
        intr = _get_intr_eof()[0]
        return self._writeb(intr), intr

    def eof(self) -> bool:
        """This returns True if the EOF exception was ever raised."""

        return self.flag_eof

    def terminate(self, force: bool = False) -> bool:
        """This forces a child process to terminate. It starts nicely with
        SIGHUP and SIGINT. If "force" is True then moves onto SIGKILL. This
        returns True if the child was terminated. This returns False if the
        child could not be terminated."""

        if not self.isalive():
            return True
        try:
            self.kill(signal.SIGHUP)
            time.sleep(self.delayafterterminate)
            if not self.isalive():
                return True
            self.kill(signal.SIGCONT)
            time.sleep(self.delayafterterminate)
            if not self.isalive():
                return True
            self.kill(signal.SIGINT)
            time.sleep(self.delayafterterminate)
            if not self.isalive():
                return True
            if force:
                self.kill(signal.SIGKILL)
                time.sleep(self.delayafterterminate)
                if not self.isalive():
                    return True
                else:
                    return False
            return False
        except OSError:
            # I think there are kernel timing issues that sometimes cause
            # this to happen. I think isalive() reports True, but the
            # process is dead to the kernel.
            # Make one last attempt to see if the kernel is up to date.
            time.sleep(self.delayafterterminate)
            if not self.isalive():
                return True
            else:
                return False

    def wait(self) -> int | None:
        """This waits until the child exits. This is a blocking call. This will
        not read any data from the child, so this will block forever if the
        child has unread output and has terminated. In other words, the child
        may have printed output then called exit(), but, the child is
        technically still alive until its output is read by the parent."""

        if self.isalive():
            _, status = os.waitpid(self.pid, 0)
        else:
            return self.exitstatus
        self.exitstatus = os.WEXITSTATUS(status)
        if os.WIFEXITED(status):
            self.status = status
            self.exitstatus = os.WEXITSTATUS(status)
            self.signalstatus = None
            self.terminated = True
        elif os.WIFSIGNALED(status):
            self.status = status
            self.exitstatus = None
            self.signalstatus = os.WTERMSIG(status)
            self.terminated = True
        elif os.WIFSTOPPED(status):  # pragma: no cover
            # You can't call wait() on a child process in the stopped state.
            raise PtyProcessError(
                "Called wait() on a stopped child "
                + "process. This is not supported. Is some other "
                + "process attempting job control with our child pid?"
            )
        return self.exitstatus

    def isalive(self) -> bool:
        """This tests if the child process is running or not. This is
        non-blocking. If the child was terminated then this will read the
        exitstatus or signalstatus of the child. This returns True if the child
        process appears to be running or False if not. It can take literally
        SECONDS for Solaris to return the right status."""

        if self.terminated:
            return False

        if self.flag_eof:
            # This is for Linux, which requires the blocking form
            # of waitpid to get the status of a defunct process.
            # This is super-lame. The flag_eof would have been set
            # in read_nonblocking(), so this should be safe.
            waitpid_options = 0
        else:
            waitpid_options = os.WNOHANG

        try:
            pid, status = os.waitpid(self.pid, waitpid_options)
        except OSError as e:
            # No child processes
            if e.errno == errno.ECHILD:
                raise PtyProcessError(
                    "isalive() encountered condition "
                    + 'where "terminated" is 0, but there was no child '
                    + "process. Did someone else call waitpid() "
                    + "on our process?"
                )
            else:
                raise

        # I have to do this twice for Solaris.
        # I can't even believe that I figured this out...
        # If waitpid() returns 0 it means that no child process
        # wishes to report, and the value of status is undefined.
        if pid == 0:
            try:
                ### os.WNOHANG) # Solaris!
                pid, status = os.waitpid(self.pid, waitpid_options)
            except OSError as e:  # pragma: no cover
                # This should never happen...
                if e.errno == errno.ECHILD:
                    raise PtyProcessError(
                        "isalive() encountered condition "
                        + "that should never happen. There was no child "
                        + "process. Did someone else call waitpid() "
                        + "on our process?"
                    )
                else:
                    raise

            # If pid is still 0 after two calls to waitpid() then the process
            # really is alive. This seems to work on all platforms, except for
            # Irix which seems to require a blocking call on waitpid or select,
            # so I let read_nonblocking take care of this situation
            # (unfortunately, this requires waiting through the timeout).
            if pid == 0:
                return True

        if pid == 0:
            return True

        if os.WIFEXITED(status):
            self.status = status
            self.exitstatus = os.WEXITSTATUS(status)
            self.signalstatus = None
            self.terminated = True
        elif os.WIFSIGNALED(status):
            self.status = status
            self.exitstatus = None
            self.signalstatus = os.WTERMSIG(status)
            self.terminated = True
        elif os.WIFSTOPPED(status):
            raise PtyProcessError(
                "isalive() encountered condition "
                + "where child process is stopped. This is not "
                + "supported. Is some other process attempting "
                + "job control with our child pid?"
            )
        return False

    def kill(self, sig: int) -> None:
        """Send the given signal to the child application.

        In keeping with UNIX tradition it has a misleading name. It does not
        necessarily kill the child unless you send the right signal. See the
        :mod:`signal` module for constants representing signal numbers.
        """

        # Same as os.kill, but the pid is given for you.
        if self.isalive():
            os.kill(self.pid, sig)

    def getwinsize(self) -> tuple[int, int]:
        """Return the window size of the pseudoterminal as a tuple (rows, cols)."""
        TIOCGWINSZ = getattr(termios, "TIOCGWINSZ", 1074295912)
        s = struct.pack("HHHH", 0, 0, 0, 0)
        x = fcntl.ioctl(self.fd, TIOCGWINSZ, s)
        return struct.unpack("HHHH", x)[0:2]

    def setwinsize(self, rows: int, cols: int) -> None:
        """Set the terminal window size of the child tty.

        This will cause a SIGWINCH signal to be sent to the child. This does not
        change the physical window size. It changes the size reported to
        TTY-aware applications like vi or curses -- applications that respond to
        the SIGWINCH signal.
        """
        return _setwinsize(self.fd, rows, cols)


class PtyProcessUnicode(PtyProcess):
    """Unicode wrapper around a process running in a pseudoterminal.

    This class exposes a similar interface to :class:`PtyProcess`, but its read
    methods return unicode, and its :meth:`write` accepts unicode.
    """

    def __init__(
        self,
        pid: int,
        fd: int,
        argv: list | None = None,
        env: Mapping | None = None,
        cwd: str | None = None,
    ):
        super().__init__(pid, fd, argv, env, cwd)
        self.encoding = "utf-8"
        self.codec_errors = "strict"
        self.decoder = codecs.getincrementaldecoder(self.encoding)(
            errors=self.codec_errors
        )

    def read(self, size: int = 1024) -> str | bytes:
        """Read at most ``size`` bytes from the pty, return them as unicode.

        Can block if there is nothing to read. Raises :exc:`EOFError` if the
        terminal was closed.

        The size argument still refers to bytes, not unicode code points.
        """
        b = super(PtyProcessUnicode, self).read(size)
        assert isinstance(b, bytes)
        return self.decoder.decode(b, final=False)

    def readline(self) -> str | bytes:
        """Read one line from the pseudoterminal, and return it as unicode.

        Can block if there is nothing to read. Raises :exc:`EOFError` if the
        terminal was closed.
        """
        b = super(PtyProcessUnicode, self).readline()
        assert isinstance(b, bytes)
        return self.decoder.decode(b, final=False)

    def write(self, s: str | bytes, flush: bool = True) -> int:
        """Write the unicode string ``s`` to the pseudoterminal.

        Returns the number of bytes written.
        """
        assert isinstance(s, str)
        b = s.encode(self.encoding)
        return super(PtyProcessUnicode, self).write(b, flush)
