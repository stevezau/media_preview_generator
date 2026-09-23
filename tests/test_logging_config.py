"""
Tests for logging configuration.
"""

import json
import logging
import os
import sys
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

import media_preview_generator.logging_config as _logging_mod
from media_preview_generator.logging_config import (
    _json_sink,
    _jsonl_record_patcher,
    get_app_log_path,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _reset_logging_state():
    """Reset setup_logging module globals AND loguru sinks between tests.

    Without snapshotting loguru's own _core.handlers, tests that call
    setup_logging() (which removes existing handlers and adds new ones
    bound to test-scoped StringIO objects) leave the global loguru in a
    broken state. Background threads from other test modules — the job
    dispatcher, scheduler, retry queue, webhook timers — keep emitting
    into those torn-down sinks and can crash with
    "I/O operation on closed file" when the captured StringIO is GC'd.

    Snapshot loguru's handler set on entry, restore on exit so the
    next test (or a daemon thread that survives this test) sees a sane
    sink configuration.
    """
    from loguru import logger as _loguru_logger

    snapshot = dict(_loguru_logger._core.handlers)  # noqa: SLF001
    # setup_logging installs a global patcher; loguru's configure(patcher=None) leaves it in place, so put it back here.
    patcher = _loguru_logger._core.patcher  # noqa: SLF001
    # ...and a handler on the standard library's root logger (stdlib records into loguru).
    stdlib_root_handlers = list(logging.root.handlers)
    logging.root.handlers = [h for h in stdlib_root_handlers if not isinstance(h, _logging_mod._StdlibToLoguru)]
    _logging_mod._managed_handler_ids = []
    _logging_mod._initial_setup_done = False
    old_broadcaster = _logging_mod._broadcaster
    _logging_mod._broadcaster = None
    try:
        yield
    finally:
        logging.root.handlers = stdlib_root_handlers
        _logging_mod._managed_handler_ids = []
        _logging_mod._initial_setup_done = False
        _logging_mod._broadcaster = old_broadcaster
        _loguru_logger._core.patcher = patcher  # noqa: SLF001
        # Restore loguru's pre-test handler set so test-scoped StringIO
        # sinks can't outlive the test that created them.
        _loguru_logger.remove()
        for handler in snapshot.values():
            try:
                _loguru_logger.add(
                    handler._sink,  # noqa: SLF001
                    level=handler.levelno,
                )
            except Exception:
                # Best-effort restore; some handlers (Rich console) carry
                # state that doesn't round-trip cleanly. The next call to
                # setup_logging() in production code will re-establish
                # the production sinks anyway.
                pass


class TestLoggingConfig:
    """Test logging configuration."""

    @patch("media_preview_generator.logging_config.os.makedirs")
    @patch("media_preview_generator.logging_config.logger")
    def test_setup_logging_default(self, mock_logger, mock_makedirs):
        """Default level: stderr handler at INFO + JSONL app.log handler."""
        setup_logging()

        mock_logger.remove.assert_called_once()
        # Expect stderr + app.log handlers (2 add calls minimum).
        assert mock_logger.add.call_count == 2
        stderr_call = mock_logger.add.call_args_list[0]
        assert stderr_call.kwargs.get("level") == "INFO"

    @patch("media_preview_generator.logging_config.os.makedirs")
    @patch("media_preview_generator.logging_config.logger")
    def test_setup_logging_debug(self, mock_logger, mock_makedirs):
        """DEBUG level propagates to the stderr handler config."""
        setup_logging("DEBUG")

        mock_logger.remove.assert_called_once()
        assert mock_logger.add.call_count == 2
        stderr_call = mock_logger.add.call_args_list[0]
        assert stderr_call.kwargs.get("level") == "DEBUG"
        # The app.log handler must keep rotation/retention even at DEBUG.
        app_log_call = mock_logger.add.call_args_list[1]
        assert app_log_call.kwargs.get("rotation") == "10 MB"
        assert app_log_call.kwargs.get("retention") == 5

    @patch("media_preview_generator.logging_config.os.makedirs")
    @patch("media_preview_generator.logging_config.logger")
    def test_setup_logging_with_console(self, mock_logger, mock_makedirs):
        """Passing a Rich console should bind the stderr handler to it.

        Strengthened: prove the sink actually routes to ``console.print``
        instead of just trusting that the level + handler-count checks
        imply correct wiring. Production wraps the console in a
        ``lambda msg: console.print(msg, end="")`` (logging_config.py:181) —
        invoke the bound sink and verify console.print was called.
        """
        mock_console = MagicMock()

        setup_logging("INFO", console=mock_console)

        mock_logger.remove.assert_called_once()
        assert mock_logger.add.call_count == 2
        stderr_call = mock_logger.add.call_args_list[0]
        assert stderr_call.kwargs.get("level") == "INFO"

        # The sink must be a callable that forwards to console.print —
        # invoke it and assert the console was actually called. A regression
        # that bound the sink to sys.stderr (ignoring the console arg)
        # would fail here.
        sink = stderr_call.args[0]
        assert callable(sink), f"first add() arg must be a callable sink, got {type(sink).__name__}"
        sink("test log message\n")
        mock_console.print.assert_called_once_with("test log message\n", end="")

    @patch("media_preview_generator.logging_config.os.makedirs")
    @patch("media_preview_generator.logging_config.logger")
    def test_setup_logging_adds_app_log_handler(self, mock_logger, mock_makedirs):
        """Test that setup_logging adds the consolidated JSONL app.log handler."""
        setup_logging()

        # stderr + app.log = 2 handlers
        assert mock_logger.add.call_count == 2

        app_log_call = mock_logger.add.call_args_list[1]
        assert app_log_call.kwargs.get("level") == "INFO"
        assert app_log_call.kwargs.get("rotation") == "10 MB"
        assert app_log_call.kwargs.get("retention") == 5
        # A callable format: a string one makes loguru append its own (unmasked) exception text.
        assert app_log_call.kwargs.get("format")({}) == "{extra[_jsonl]}\n{extra[_traceback]}"

    @patch("media_preview_generator.logging_config.os.makedirs")
    @patch("media_preview_generator.logging_config.logger")
    def test_setup_logging_custom_rotation_retention(self, mock_logger, mock_makedirs):
        """Test setup_logging with custom rotation and retention values."""
        setup_logging(rotation="5 MB", retention=4)

        assert mock_logger.add.call_count == 2

        app_log_call = mock_logger.add.call_args_list[1]
        assert app_log_call.kwargs.get("rotation") == "5 MB"
        assert app_log_call.kwargs.get("retention") == 4

    @patch("media_preview_generator.logging_config.os.makedirs", side_effect=PermissionError)
    @patch("media_preview_generator.logging_config.logger")
    def test_setup_logging_handles_permission_error(self, mock_logger, mock_makedirs):
        """Test that setup_logging handles permission errors for log directory."""
        setup_logging()

        # Should still add stderr handler but not error file handler
        mock_logger.remove.assert_called_once()
        assert mock_logger.add.call_count == 1

    def test_a_logged_exception_shows_no_local_values(self, tmp_path):
        """loguru's ``diagnose`` prints each frame's variable values under a traceback; a token among them would land
        in ``docker logs`` and app.log. The traceback itself stays."""
        from loguru import logger

        printed: list[str] = []
        console = MagicMock()
        console.print.side_effect = lambda msg, end="": printed.append(str(msg))
        with patch.dict(os.environ, {"CONFIG_DIR": str(tmp_path)}):
            setup_logging("INFO", console=console)

        def fetch(token):
            raise ConnectionError(f"no answer after {len(token)} characters")

        try:
            fetch("s3cr3t" + "-t0ken")  # not a literal: the traceback quotes this line's source
        except ConnectionError:
            logger.exception("fetch failed")
        logger.complete()

        for text in ("".join(printed), (tmp_path / "logs" / "app.log").read_text()):
            assert "Traceback" in text and "no answer after 12 characters" in text
            assert "s3cr3t-t0ken" not in text

    def test_a_token_in_a_log_message_is_masked_in_every_handler(self, tmp_path):
        from loguru import logger

        printed: list[str] = []
        console = MagicMock()
        console.print.side_effect = lambda msg, end="": printed.append(str(msg))
        socketio = MagicMock()
        _logging_mod._broadcaster = _logging_mod.SocketIOLogBroadcaster(socketio)
        with patch.dict(os.environ, {"CONFIG_DIR": str(tmp_path)}):
            setup_logging("INFO", console=console)

        logger.warning("Couldn't reach {}", "http://plex:32400/?X-Plex-Token=" + "s3cr3t")
        logger.complete()

        live = " ".join(c.args[1]["msg"] for c in socketio.emit.call_args_list)
        for text in ("".join(printed), (tmp_path / "logs" / "app.log").read_text(), live):
            assert "X-Plex-Token=****" in text and "s3cr3t" not in text

    @pytest.mark.parametrize("console_kind", ["rich-console", "stderr", "json"])
    def test_a_logged_exceptions_own_text_is_masked_in_every_handler(self, tmp_path, console_kind):
        """requests' ConnectionError text carries the request URL, query token included; the patcher only masks the
        message, so each handler masks the traceback it writes (chained causes too)."""
        from loguru import logger

        printed: list[str] = []
        console = None
        if console_kind == "rich-console":
            console = MagicMock()
            console.print.side_effect = lambda msg, end="": printed.append(str(msg))
        stderr = StringIO()
        token = "s3cr3t" + "XYZ"  # not a literal: the traceback quotes the raising lines' source
        with (
            patch.dict(os.environ, {"CONFIG_DIR": str(tmp_path)}),
            patch("media_preview_generator.logging_config.sys.stderr", stderr),
        ):
            setup_logging("INFO", console=console, log_format="json" if console_kind == "json" else "pretty")
            try:
                try:
                    raise OSError(f"GET http://jf:8096/Items?api_key={token}")
                except OSError as inner:
                    raise ConnectionError(f"GET /library/metadata/1?X-Plex-Token={token} failed") from inner
            except ConnectionError:
                logger.exception("fetch failed")
            logger.complete()

        console_text = "".join(printed) if console_kind == "rich-console" else stderr.getvalue()
        app_log = (tmp_path / "logs" / "app.log").read_text()
        for text in (console_text, app_log):
            assert "X-Plex-Token=****" in text and "s3cr3tXYZ" not in text
        assert "Traceback" in app_log and '"msg": "fetch failed"' in app_log and "api_key=****" in app_log
        if console_kind != "json":  # the JSON line carries the exception's repr, not its traceback
            assert "Traceback" in console_text and "api_key=****" in console_text

    def test_setup_logging_creates_error_log(self, tmp_path):
        """Test that setup_logging creates the error log file on disk."""
        from loguru import logger

        with patch.dict(os.environ, {"CONFIG_DIR": str(tmp_path)}):
            # Reset logger state
            logger.remove()
            setup_logging()

        # Log directory should have been created
        log_dir = str(tmp_path / "logs")
        assert os.path.isdir(log_dir)

        # Clean up handlers we added
        logger.remove()


class TestStdlibLogging:
    """Standard-library ``logging`` (Flask, werkzeug, APScheduler, python-socketio, …) goes through loguru, masked and
    written like the app's own lines, at the levels that reached the console before: the stdlib's last-resort handler
    printed WARNING and above of loggers no handler took, and a library's own handler (urllib3's NullHandler) kept its
    records.
    """

    TOKEN_URL = "http://plex:32400/library?X-Plex-Token=" + "s3cr3t"

    @staticmethod
    def _app_log_line(app_log, text):
        """app.log's JSON line whose message holds ``text``."""
        [line] = [json.loads(ln) for ln in app_log.splitlines() if ln.startswith("{") and text in json.loads(ln)["msg"]]
        return line

    @pytest.fixture
    def start(self, tmp_path):
        """``start(level)`` runs setup_logging (INFO, the app's default, unless given) with a console, app.log and the
        live viewer, and returns ``read()``: what each of the three wrote so far."""
        from loguru import logger

        printed: list[str] = []
        console = MagicMock()
        console.print.side_effect = lambda msg, end="": printed.append(str(msg))
        socketio = MagicMock()
        _logging_mod._broadcaster = _logging_mod.SocketIOLogBroadcaster(socketio)

        def read():
            logger.complete()
            app_log = tmp_path / "logs" / "app.log"
            live = " ".join(c.args[1]["msg"] for c in socketio.emit.call_args_list)
            return "".join(printed), (app_log.read_text() if app_log.exists() else ""), live

        def run(level="INFO"):
            with patch.dict(os.environ, {"CONFIG_DIR": str(tmp_path)}):
                setup_logging(level, console=console)
            return read

        return run

    @pytest.fixture
    def stdlib_logger(self, request):
        """``stdlib_logger(suffix)``: a logger of this test's own, left without handlers afterwards."""
        made: list[logging.Logger] = []

        def make(suffix):
            made.append(logging.getLogger(f"tests.stdlib.{request.node.name}.{suffix}"))
            return made[-1]

        yield make
        for lg in made:
            lg.handlers.clear()
            lg.setLevel(logging.NOTSET)
            lg.propagate = True

    def test_a_stdlib_warning_reaches_every_sink_masked_at_its_level_naming_its_caller(self, start, stdlib_logger):
        read = start()
        stdlib_logger("plain").warning("Couldn't reach %s", self.TOKEN_URL)
        texts = read()
        for text in texts:
            assert "Couldn't reach http://plex:32400/library?X-Plex-Token=****" in text and "s3cr3t" not in text
        line = self._app_log_line(texts[1], "Couldn't reach")
        assert (line["level"], line["mod"], line["func"]) == (
            "WARNING",
            "test_logging_config",
            "test_a_stdlib_warning_reaches_every_sink_masked_at_its_level_naming_its_caller",
        )

    def test_a_stdlib_exceptions_traceback_is_masked(self, start, stdlib_logger):
        read = start()
        token = "s3cr3t" + "XYZ"  # not a literal: the traceback quotes the raising line's source
        try:
            raise ConnectionError(f"GET /library/metadata/1?X-Plex-Token={token} failed")
        except ConnectionError:
            stdlib_logger("exc").exception("fetch failed")
        console, app_log, _live = read()
        for text in (console, app_log):
            assert "fetch failed" in text and "Traceback" in text
            assert "X-Plex-Token=****" in text and "s3cr3tXYZ" not in text
        assert self._app_log_line(app_log, "fetch failed")["level"] == "ERROR"

    def test_a_level_loguru_doesnt_know_keeps_its_number(self, start, stdlib_logger):
        logging.addLevelName(35, "NOTICE")  # a library's own level name
        read = start()
        stdlib_logger("custom").log(35, "a notice")
        _console, app_log, _live = read()
        assert self._app_log_line(app_log, "a notice")["level"] == "Level 35"

    @pytest.mark.parametrize("logger_before_setup", [False, True], ids=["logger-made-after", "logger-made-before"])
    def test_a_flask_route_traceback_is_written_once_through_loguru(self, start, capsys, logger_before_setup):
        # Flask gives its app logger a stderr handler of its own when nothing handles its records yet.
        from flask import Flask

        app = Flask(f"tests.stdlib.flask_{logger_before_setup}")
        token_url = self.TOKEN_URL

        @app.route("/boom")
        def boom():
            raise RuntimeError(f"upstream said no: {token_url}")

        if logger_before_setup:
            from flask.logging import default_handler

            # What Flask attaches on first use when nothing handles its records (pytest's own capture handler on the
            # root logger hides that from Flask's check here, so it is attached by hand).
            app.logger.addHandler(default_handler)
        read = start()
        try:
            assert app.test_client().get("/boom").status_code == 500
            console, app_log, _live = read()
            assert console.count("Exception on /boom [GET]") == 1 and app_log.count("Exception on /boom [GET]") == 1
            assert "upstream said no: http://plex:32400/library?X-Plex-Token=****" in console
            assert "s3cr3t" not in console + app_log
            assert "Exception on /boom" not in capsys.readouterr().err  # Flask's own handler printed nothing
        finally:
            app.logger.handlers.clear()

    @pytest.mark.parametrize("stream", ["stderr", "stdout"])
    def test_a_librarys_own_console_handler_is_taken_off_so_its_lines_are_written_once(
        self, start, stdlib_logger, capsys, stream
    ):
        lib = stdlib_logger("socketio_like")  # python-socketio and python-engineio set themselves up like this
        lib.setLevel(logging.ERROR)
        lib.addHandler(logging.StreamHandler(getattr(sys, stream)))
        read = start()
        lib.error("emit failed for %s", self.TOKEN_URL)
        lib.warning("below the library's own level")
        console, _app_log, _live = read()
        assert console.count("emit failed for http://plex:32400/library?X-Plex-Token=****") == 1
        assert "below the library's own level" not in console
        assert capsys.readouterr() == ("", "")
        assert lib.handlers == []

    @pytest.mark.parametrize("kind", ["stream-of-its-own", "file"])
    def test_a_handler_writing_elsewhere_than_the_console_is_kept(self, start, stdlib_logger, tmp_path, kind):
        # Only a console handler would print a line twice: a handler with somewhere else to write keeps its records.
        lib = stdlib_logger("own_output")
        if kind == "file":
            handler = logging.FileHandler(tmp_path / "lib.log")
        else:
            handler = logging.StreamHandler(StringIO())
        lib.addHandler(handler)
        read = start()
        lib.warning("kept where the library writes")
        console, app_log, _live = read()
        assert lib.handlers == [handler]
        handler.flush()
        written = (tmp_path / "lib.log").read_text() if kind == "file" else handler.stream.getvalue()
        assert "kept where the library writes" in written
        assert "kept where the library writes" not in console + app_log
        handler.close()

    def test_a_logger_that_doesnt_propagate_keeps_its_own_handler(self, start, stdlib_logger):
        # gunicorn's own loggers: they write where gunicorn was told to, and never reach the root logger.
        own = StringIO()
        gunicorn_error = stdlib_logger("gunicorn_error")
        gunicorn_error.propagate = False
        gunicorn_error.addHandler(logging.StreamHandler(own))
        read = start()
        gunicorn_error.error("Worker (pid:7) was sent SIGKILL")
        console, _app_log, _live = read()
        assert "SIGKILL" in own.getvalue() and "SIGKILL" not in console
        assert len(gunicorn_error.handlers) == 1

    @pytest.mark.parametrize("root_level", [None, logging.DEBUG], ids=["root-level-as-is", "root-level-lowered"])
    def test_levels_stay_what_reached_the_console_before(self, start, stdlib_logger, root_level):
        # A lowered root level (pytest's caplog does it; so would a library's logging.basicConfig) still lets no
        # library's DEBUG or INFO line through: the last-resort handler printed WARNING and above only.
        import urllib3  # noqa: F401 - gives the urllib3 logger its NullHandler, as importing requests does

        before = logging.root.level
        read = start("DEBUG")  # the app's own DEBUG lines are on; the libraries' aren't
        assert logging.root.level == before
        if root_level is not None:
            logging.root.setLevel(root_level)
        try:
            plain = stdlib_logger("plain")
            plain.info("an info line")
            plain.debug("a debug line")
            logging.getLogger("urllib3.connectionpool").debug("Starting new HTTP connection (1): plex:32400")
            logging.getLogger("urllib3.connectionpool").warning("Retrying (Retry(total=2)) after connection broken")
            logging.getLogger("werkzeug").info('127.0.0.1 - - "GET /api/jobs HTTP/1.1" 200 -')
            plain.warning("a warning line")
        finally:
            logging.root.setLevel(before)
        written = " ".join(read())
        for quiet in ("an info line", "a debug line", "Starting new HTTP", "Retrying (Retry", "GET /api/jobs"):
            assert quiet not in written
        assert "a warning line" in written

    def test_setting_up_again_installs_one_handler_and_writes_each_line_once(self, start, stdlib_logger):
        start()
        read = start()  # changing the log level in Settings sets logging up again
        stdlib_logger("twice").warning("only once please")
        console, app_log, live = read()
        assert sum(isinstance(h, _logging_mod._StdlibToLoguru) for h in logging.root.handlers) == 1
        assert [text.count("only once please") for text in (console, app_log, live)] == [1, 1, 1]

    @pytest.mark.parametrize("started_in", ["stdlib", "loguru"])
    @pytest.mark.parametrize("enqueue", [False, True], ids=["sink-in-the-caller", "sink-on-loguru-s-writer-thread"])
    def test_a_sink_that_hands_records_back_to_logging_writes_each_line_once(
        self, start, stdlib_logger, capfd, started_in, enqueue
    ):
        # The tests' caplog bridge (tests/markers/conftest.py loguru_caplog) is such a sink: every loguru record goes
        # back into stdlib logging, where the root handler must not hand it to loguru again.
        from loguru import logger

        handed_back: list[str] = []

        class BackToLogging(logging.Handler):
            def emit(self, record):
                handed_back.append(record.getMessage())
                if len(handed_back) <= 5:  # a loop stops here instead of hanging the test
                    logging.getLogger(record.name).handle(record)

        read = start()
        handler_id = logger.add(BackToLogging(), level="WARNING", format="{message}", enqueue=enqueue)
        try:
            if started_in == "stdlib":
                stdlib_logger("loop").warning("round trip")
            else:
                logger.warning("round trip")
            logger.complete()
        finally:
            logger.remove(handler_id)
        console, _app_log, _live = read()
        assert console.count("round trip") == 1
        assert handed_back == ["round trip"]
        assert "deadlock" not in capfd.readouterr().err

    def test_a_sink_that_logs_through_logging_itself_writes_each_line_once(self, start, stdlib_logger):
        # Not loguru's handler sink: the record it logs is a new one, so only the handing-over guard stops the round.
        # Only on the caller's thread: an enqueue sink doing this would still loop, but no sink of the app's logs
        # through ``logging`` (the live viewer's socketio logs at ERROR only on its own failures).
        from loguru import logger

        relog = stdlib_logger("relog")
        relogged: list[str] = []

        def sink(message):
            relogged.append(message.record["message"])
            if len(relogged) <= 5:  # a loop stops here instead of recursing without end
                relog.warning(message.record["message"])

        read = start()
        handler_id = logger.add(sink, level="WARNING", format="{message}")
        try:
            stdlib_logger("origin").warning("said once")
        finally:
            logger.remove(handler_id)
        console, _app_log, _live = read()
        assert console.count("said once") == 1
        assert relogged == ["said once"]


# -----------------------------------------------------------------------
# Structured JSON logging (Item 36)
# -----------------------------------------------------------------------


class TestJSONLogging:
    """Test LOG_FORMAT=json structured logging output."""

    @patch("media_preview_generator.logging_config.os.makedirs")
    @patch("media_preview_generator.logging_config.logger")
    def test_json_format_adds_json_sink(self, mock_logger, mock_makedirs):
        """When log_format='json', _json_sink should be registered."""
        setup_logging(log_format="json")
        mock_logger.remove.assert_called_once()

        # First add call should use _json_sink
        first_add = mock_logger.add.call_args_list[0]
        assert first_add.args[0] is _json_sink

    @patch("media_preview_generator.logging_config.os.makedirs")
    @patch("media_preview_generator.logging_config.logger")
    def test_json_format_via_env_var(self, mock_logger, mock_makedirs):
        """LOG_FORMAT env var should be respected when log_format is None."""
        with patch.dict(os.environ, {"LOG_FORMAT": "json"}):
            setup_logging()
        first_add = mock_logger.add.call_args_list[0]
        assert first_add.args[0] is _json_sink

    @patch("media_preview_generator.logging_config.os.makedirs")
    @patch("media_preview_generator.logging_config.logger")
    def test_pretty_format_ignores_json_sink(self, mock_logger, mock_makedirs):
        """Explicit log_format='pretty' should NOT use _json_sink."""
        setup_logging(log_format="pretty")
        first_add = mock_logger.add.call_args_list[0]
        assert first_add.args[0] is not _json_sink

    @patch("media_preview_generator.logging_config.os.makedirs")
    @patch("media_preview_generator.logging_config.logger")
    def test_console_ignored_when_json(self, mock_logger, mock_makedirs):
        """Providing a console alongside json format should still use JSON."""
        mock_console = MagicMock()
        setup_logging(log_format="json", console=mock_console)
        first_add = mock_logger.add.call_args_list[0]
        assert first_add.args[0] is _json_sink

    def test_json_sink_produces_valid_json(self, tmp_path):
        """_json_sink should write valid JSON lines to stderr."""
        from loguru import logger

        captured = StringIO()
        logger.remove()

        with patch("media_preview_generator.logging_config.sys.stderr", captured):
            with patch.dict(os.environ, {"CONFIG_DIR": str(tmp_path)}):
                setup_logging(log_format="json")
                logger.info("hello structured world")
                logger.complete()

        logger.remove()
        output = captured.getvalue().strip()
        assert output, "Expected JSON output on stderr"

        # Find the line containing our test message (background threads may
        # inject other log lines, so we can't assume a fixed position).
        record = None
        for line in output.splitlines():
            try:
                parsed = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if "hello structured world" in parsed.get("message", ""):
                record = parsed
                break
        assert record is not None, f"Expected JSON line with 'hello structured world' in output:\n{output}"
        assert record["level"] == "INFO"
        assert "timestamp" in record
        assert "function" in record

    def test_json_sink_includes_exception(self, tmp_path):
        """When an exception is logged, it should appear in the JSON payload."""
        from loguru import logger

        captured = StringIO()
        logger.remove()

        with patch("media_preview_generator.logging_config.sys.stderr", captured):
            with patch.dict(os.environ, {"CONFIG_DIR": str(tmp_path)}):
                setup_logging(log_format="json")
                try:
                    raise ValueError("boom")
                except ValueError:
                    logger.exception("caught error")
                logger.complete()

        logger.remove()
        output = captured.getvalue().strip()
        lines = output.splitlines()
        # Find the line with "caught error"
        for line in lines:
            record = json.loads(line)
            if "caught error" in record["message"]:
                assert "exception" in record
                assert "boom" in record["exception"]
                break
        else:
            pytest.fail("Expected 'caught error' log line in JSON output")


# -----------------------------------------------------------------------
# SocketIO live-log broadcaster
# -----------------------------------------------------------------------


class TestSocketIOLogBroadcaster:
    """Tests for the SocketIOLogBroadcaster and its module-level accessors."""

    def test_get_set_broadcaster(self):
        """get/set_log_broadcaster round-trips correctly."""
        from media_preview_generator.logging_config import (
            SocketIOLogBroadcaster,
            get_log_broadcaster,
            set_log_broadcaster,
        )

        assert get_log_broadcaster() is None
        mock_sio = MagicMock()
        b = SocketIOLogBroadcaster(mock_sio)
        set_log_broadcaster(b)
        assert get_log_broadcaster() is b
        set_log_broadcaster(None)
        assert get_log_broadcaster() is None

    def test_sink_emits_to_correct_room(self):
        """sink() should call socketio.emit with room=level_name."""
        from media_preview_generator.logging_config import SocketIOLogBroadcaster

        mock_sio = MagicMock()
        broadcaster = SocketIOLogBroadcaster(mock_sio)

        record = MagicMock()
        record.record = {
            "level": MagicMock(name="WARNING"),
            "time": MagicMock(),
            "message": "test warning",
            "name": "media_preview_generator.worker",
            "function": "run",
            "line": 42,
        }
        record.record["level"].name = "WARNING"
        record.record["time"].strftime.return_value = "2026-03-22 09:10:18.123"

        broadcaster.sink(record)

        mock_sio.emit.assert_called_once()
        call_kwargs = mock_sio.emit.call_args
        assert call_kwargs.args[0] == "log_message"
        assert call_kwargs.kwargs["namespace"] == "/logs"
        assert call_kwargs.kwargs["room"] == "WARNING"

        payload = call_kwargs.args[1]
        assert payload["level"] == "WARNING"
        assert payload["msg"] == "test warning"
        assert payload["mod"] == "worker"

    def test_sink_filters_out_trace_level(self):
        """sink() should silently drop TRACE-level records."""
        from media_preview_generator.logging_config import SocketIOLogBroadcaster

        mock_sio = MagicMock()
        broadcaster = SocketIOLogBroadcaster(mock_sio)

        record = MagicMock()
        record.record = {
            "level": MagicMock(name="TRACE"),
            "time": MagicMock(),
            "message": "trace msg",
            "name": "x",
            "function": "f",
            "line": 1,
        }
        record.record["level"].name = "TRACE"

        broadcaster.sink(record)
        mock_sio.emit.assert_not_called()

    def test_sink_swallows_emit_errors(self):
        """sink() must not raise when socketio.emit fails."""
        from media_preview_generator.logging_config import SocketIOLogBroadcaster

        mock_sio = MagicMock()
        mock_sio.emit.side_effect = RuntimeError("boom")
        broadcaster = SocketIOLogBroadcaster(mock_sio)

        record = MagicMock()
        record.record = {
            "level": MagicMock(name="INFO"),
            "time": MagicMock(),
            "message": "test",
            "name": "mod",
            "function": "fn",
            "line": 1,
        }
        record.record["level"].name = "INFO"
        record.record["time"].strftime.return_value = "2026-03-22 09:10:18.123"

        broadcaster.sink(record)  # should not raise

    @patch("media_preview_generator.logging_config.os.makedirs")
    @patch("media_preview_generator.logging_config.logger")
    def test_setup_logging_attaches_broadcaster(self, mock_logger, mock_makedirs):
        """When a broadcaster is registered, setup_logging adds it as a handler."""
        from media_preview_generator.logging_config import (
            SocketIOLogBroadcaster,
            set_log_broadcaster,
        )

        mock_sio = MagicMock()
        set_log_broadcaster(SocketIOLogBroadcaster(mock_sio))

        setup_logging()

        # 2 base handlers (stderr + app.log) + 1 broadcaster = 3
        assert mock_logger.add.call_count == 3
        broadcaster_call = mock_logger.add.call_args_list[2]
        assert broadcaster_call.kwargs.get("level") == "INFO"


# -----------------------------------------------------------------------
# JSONL file sink and app.log helpers
# -----------------------------------------------------------------------


class TestJsonlRecordPatcher:
    """Tests for the _jsonl_record_patcher filter and get_app_log_path."""

    def test_patcher_stores_jsonl_in_extra(self):
        """_jsonl_record_patcher should store a valid JSON string in extra._jsonl."""
        record = {
            "time": MagicMock(),
            "level": MagicMock(),
            "message": "hello world",
            "name": "media_preview_generator.worker",
            "function": "run",
            "line": 42,
            "extra": {},
        }
        record["time"].strftime.return_value = "2026-03-22 09:10:18.123000"
        record["level"].name = "INFO"

        result = _jsonl_record_patcher(record)

        assert result is True
        jsonl_str = record["extra"]["_jsonl"]
        parsed = json.loads(jsonl_str)
        assert parsed["ts"] == "2026-03-22 09:10:18.123"
        assert parsed["level"] == "INFO"
        assert parsed["msg"] == "hello world"
        assert parsed["mod"] == "worker"
        assert parsed["func"] == "run"
        assert parsed["line"] == 42

    def test_patcher_handles_empty_name(self):
        """When record name is empty, mod should be empty."""
        record = {
            "time": MagicMock(),
            "level": MagicMock(),
            "message": "test",
            "name": "",
            "function": None,
            "line": 1,
            "extra": {},
        }
        record["time"].strftime.return_value = "2026-01-01 00:00:00.000000"
        record["level"].name = "DEBUG"

        _jsonl_record_patcher(record)
        parsed = json.loads(record["extra"]["_jsonl"])
        assert parsed["mod"] == ""
        assert parsed["func"] == ""

    def test_patcher_escapes_json_in_message(self):
        """Messages with quotes/backslashes must produce valid JSON."""
        record = {
            "time": MagicMock(),
            "level": MagicMock(),
            "message": 'path "C:\\Users\\test"',
            "name": "mod",
            "function": "f",
            "line": 1,
            "extra": {},
        }
        record["time"].strftime.return_value = "2026-01-01 00:00:00.000000"
        record["level"].name = "WARNING"

        _jsonl_record_patcher(record)
        parsed = json.loads(record["extra"]["_jsonl"])
        assert parsed["msg"] == 'path "C:\\Users\\test"'

    def test_get_app_log_path_default(self):
        """get_app_log_path returns /config/logs/app.log by default."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONFIG_DIR", None)
            assert get_app_log_path() == "/config/logs/app.log"

    def test_get_app_log_path_custom_config_dir(self):
        """get_app_log_path respects CONFIG_DIR."""
        with patch.dict(os.environ, {"CONFIG_DIR": "/my/config"}):
            assert get_app_log_path() == "/my/config/logs/app.log"

    def test_app_log_written_as_jsonl(self, tmp_path):
        """Integration: setup_logging writes valid JSONL lines to app.log."""
        from loguru import logger

        with patch.dict(os.environ, {"CONFIG_DIR": str(tmp_path)}):
            logger.remove()
            setup_logging(log_level="DEBUG")
            logger.info("integration test message")
            logger.complete()

        logger.remove()
        app_log = tmp_path / "logs" / "app.log"
        assert app_log.exists(), "app.log should have been created"
        lines = [ln for ln in app_log.read_text().strip().splitlines() if ln.strip()]
        assert len(lines) >= 1

        for line in lines:
            record = json.loads(line)
            assert "ts" in record
            assert "level" in record
            assert "msg" in record
