#!/usr/bin/env python
# vim: set sw=4 et:

import logging
import json
import urllib.request
import itertools
import websocket
import time
import threading
import subprocess
import tempfile
import os
import random
import brozzler
from brozzler.behaviors import Behavior
from requests.structures import CaseInsensitiveDict
import select
import re
import base64

__all__ = ["BrowserPool", "Browser"]

class BrowserPool:
    logger = logging.getLogger(__module__ + "." + __qualname__)

    BASE_PORT = 9200

    def __init__(self, size=3, **kwargs):
        """kwargs are passed on to Browser.__init__"""
        self._available = set()
        self._in_use = set()

        for i in range(0, size):
            browser = Browser(BrowserPool.BASE_PORT + i, **kwargs)
            self._available.add(browser)

        self._lock = threading.Lock()

        self.logger.info("browser ports: {}".format([browser.chrome_port for browser in self._available]))

    def acquire(self):
        """Returns browser from pool if available, raises NoBrowsersAvailable otherwise."""
        with self._lock:
            try:
                browser = self._available.pop()
            except KeyError:
                raise NoBrowsersAvailable()
            self._in_use.add(browser)
            return browser

    def release(self, browser):
        with self._lock:
            self._available.add(browser)
            self._in_use.remove(browser)

    def shutdown_now(self):
        for browser in self._in_use:
            browser.abort_browse_page()

class NoBrowsersAvailable(Exception):
    pass

class BrowsingException(Exception):
    pass

class BrowsingAborted(BrowsingException):
    pass

class Browser:
    """Runs chrome/chromium to synchronously browse one page at a time using
    worker.browse_page(). Currently the implementation starts up a new instance
    of chrome for each page browsed, always on the same debug port. (In the
    future, it may keep the browser running indefinitely.)"""

    logger = logging.getLogger(__module__ + "." + __qualname__)

    HARD_TIMEOUT_SECONDS = 20 * 60

    def __init__(self, chrome_port=9222, chrome_exe='chromium-browser', proxy=None, ignore_cert_errors=False):
        self.command_id = itertools.count(1)
        self.chrome_port = chrome_port
        self.chrome_exe = chrome_exe
        self.proxy = proxy
        self.ignore_cert_errors = ignore_cert_errors
        self._behavior = None
        self._websock = None
        self._abort_browse_page = False
        self._chrome_instance = None
        self._aw_snap_hes_dead_jim = None

    def __repr__(self):
        return "{}.{}:{}".format(Browser.__module__, Browser.__qualname__, self.chrome_port)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()

    def start(self, proxy=None):
        # these can raise exceptions
        self._work_dir = tempfile.TemporaryDirectory()
        self._chrome_instance = Chrome(port=self.chrome_port,
                executable=self.chrome_exe,
                user_home_dir=self._work_dir.name,
                user_data_dir=os.sep.join([self._work_dir.name, "chrome-user-data"]),
                ignore_cert_errors=self.ignore_cert_errors,
                proxy=proxy or self.proxy)
        self._websocket_url = self._chrome_instance.start()

    def stop(self):
        try:
            if self._chrome_instance:
                self._chrome_instance.stop()
                self._chrome_instance = None
            if self._work_dir:
                self._work_dir.cleanup()
                self._work_dir = None
        except:
            self.logger.error("problem stopping", exc_info=True)

    def abort_browse_page(self):
        self._abort_browse_page = True

    def browse_page(self, url, extra_headers=None, on_request=None,
        on_screenshot=None, on_url_change=None):
        """Synchronously loads a page, takes a screenshot, and runs behaviors.

        Raises BrowsingException if browsing the page fails in a non-critical
        way.

        Returns extracted outlinks.
        """
        self.url = url
        self.extra_headers = extra_headers
        self.on_request = on_request
        self.on_screenshot = on_screenshot
        self.on_url_change = on_url_change

        self._waiting_on_screenshot_msg_id = None
        self._waiting_on_document_url_msg_id = None
        self._waiting_on_outlinks_msg_id = None
        self._outlinks = None
        self._reached_limit = None
        self._aw_snap_hes_dead_jim = None
        self._abort_browse_page = False

        self._websock = websocket.WebSocketApp(self._websocket_url,
                on_open=self._visit_page, on_message=self._wrap_handle_message)

        threadName = "WebsockThread{}-{}".format(self.chrome_port,
                ''.join((random.choice('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(6))))
        websock_thread = threading.Thread(target=self._websock.run_forever, name=threadName, kwargs={'ping_timeout':0.5})
        websock_thread.start()
        self._start = time.time()
        aborted = False

        try:
            while True:
                before_sleep = time.time()
                time.sleep(0.5)
                after_sleep = time.time()
                if after_sleep - before_sleep > 1:
                    self.logger.warn("slept for %d seconds?!?!?! (should have been ~0.5)", (after_sleep - before_sleep))
                if self._browse_interval_func():
                    return self._outlinks
        finally:
            if self._websock and self._websock.sock and self._websock.sock.connected:
                try:
                    self._websock.close()
                except BaseException as e:
                    self.logger.error("exception closing websocket {} - {}".format(self._websock, e))

            websock_thread.join(timeout=30)
            if websock_thread.is_alive():
                self.logger.error("{} still alive 30 seconds after closing {}, will forcefully nudge it again".format(websock_thread, self._websock))
                self._websock.keep_running = False
                websock_thread.join(timeout=30)
                if websock_thread.is_alive():
                    self.logger.critical("{} still alive 60 seconds after closing {}".format(websock_thread, self._websock))

            self._behavior = None

    def _browse_interval_func(self):
        """Returns True when finished browsing."""
        if not self._websock or not self._websock.sock or not self._websock.sock.connected:
            raise BrowsingException("websocket closed, did chrome die? {}".format(self._websocket_url))
        elif self._aw_snap_hes_dead_jim:
            raise BrowsingException("""chrome tab went "aw snap" or "he's dead jim"!""")
        elif self._behavior != None and self._behavior.is_finished():
            if self._outlinks:
                self.logger.info("got outlinks, finished url={}".format(self.url))
                return True
            elif not self._waiting_on_outlinks_msg_id:
                self.logger.info("finished browsing page according to behavior, retrieving outlinks url={}".format(self.url))
                self._waiting_on_outlinks_msg_id = self.send_to_chrome(method="Runtime.evaluate",
                        params={"expression":"Array.prototype.slice.call(document.querySelectorAll('a[href]')).join(' ')"})
                self._waiting_on_outlinks_start = time.time()
                return False
            elif time.time() - self._waiting_on_outlinks_start > 300:
                raise BrowsingException("timed out after waiting {} seconds for outlinks", time.time() - self._waiting_on_outlinks_start)
            else:
                return False
        elif time.time() - self._start > Browser.HARD_TIMEOUT_SECONDS:
            self.logger.info("finished browsing page, reached hard timeout of {} seconds url={}".format(Browser.HARD_TIMEOUT_SECONDS, self.url))
            return True
        elif self._reached_limit:
            raise self._reached_limit
        elif self._abort_browse_page:
            raise BrowsingAborted("browsing page aborted")

    def send_to_chrome(self, suppress_logging=False, **kwargs):
        msg_id = next(self.command_id)
        kwargs['id'] = msg_id
        msg = json.dumps(kwargs)
        if not suppress_logging:
            self.logger.debug('sending message to {}: {}'.format(self._websock, msg))
        self._websock.send(msg)
        return msg_id

    def _visit_page(self, websock):
        # navigate to about:blank here to avoid situation where we navigate to
        # the same page that we're currently on, perhaps with a different
        # #fragment, which prevents Page.loadEventFired from happening
        self.send_to_chrome(method="Page.navigate", params={"url": "about:blank"})

        self.send_to_chrome(method="Network.enable")
        self.send_to_chrome(method="Page.enable")
        self.send_to_chrome(method="Console.enable")
        self.send_to_chrome(method="Debugger.enable")
        self.send_to_chrome(method="Runtime.enable")

        if self.extra_headers:
            self.send_to_chrome(method="Network.setExtraHTTPHeaders", params={"headers":self.extra_headers})

        # disable google analytics, see _handle_message() where breakpoint is caught "Debugger.paused"
        self.send_to_chrome(method="Debugger.setBreakpointByUrl", params={"lineNumber": 1, "urlRegex":"https?://www.google-analytics.com/analytics.js"})

        # navigate to the page!
        self.send_to_chrome(method="Page.navigate", params={"url": self.url})

    def _wrap_handle_message(self, websock, message):
        try:
            self._handle_message(websock, message)
        except:
            self.logger.error("uncaught exception in _handle_message", exc_info=True)
            self.abort_browse_page()

    def _network_request_will_be_sent(self, message):
        if self._behavior:
            self._behavior.notify_of_activity()
        if message["params"]["request"]["url"].lower().startswith("data:"):
            self.logger.debug("ignoring data url {}".format(message["params"]["request"]["url"][:80]))
        elif self.on_request:
            self.on_request(message)

    def _network_response_received(self, message):
        if (not self._reached_limit
                and message["params"]["response"]["status"] == 420
                and "Warcprox-Meta" in CaseInsensitiveDict(message["params"]["response"]["headers"])):
            warcprox_meta = json.loads(CaseInsensitiveDict(message["params"]["response"]["headers"])["Warcprox-Meta"])
            self._reached_limit = brozzler.ReachedLimit(warcprox_meta=warcprox_meta)
            self.logger.info("reached limit %s", self._reached_limit)

    def _page_load_event_fired(self, message):
        self.logger.info("Page.loadEventFired, requesting screenshot url={} message={}".format(self.url, message))
        self._waiting_on_screenshot_msg_id = self.send_to_chrome(method="Page.captureScreenshot")
        self._waiting_on_document_url_msg_id = self.send_to_chrome(method="Runtime.evaluate", params={"expression":"document.URL"})

    def _console_message_added(self, message):
        self.logger.debug("%s console.%s %s", self._websock.url,
                message["params"]["message"]["level"],
                message["params"]["message"]["text"])

    def _debugger_paused(self, message):
        # We hit the breakpoint set in visit_page. Get rid of google
        # analytics script!
        self.logger.debug("debugger paused! message={}".format(message))
        scriptId = message['params']['callFrames'][0]['location']['scriptId']

        # replace script
        self.send_to_chrome(method="Debugger.setScriptSource", params={"scriptId": scriptId, "scriptSource":"console.log('google analytics is no more!');"})

        # resume execution
        self.send_to_chrome(method="Debugger.resume")

    def _handle_result_message(self, message):
        if message["id"] == self._waiting_on_screenshot_msg_id:
            if self.on_screenshot:
                self.on_screenshot(base64.b64decode(message["result"]["data"]))
            self._waiting_on_screenshot_msg_id = None
            self.logger.info("got screenshot, moving on to starting behaviors url={}".format(self.url))
            self._behavior = Behavior(self.url, self)
            self._behavior.start()
        elif message["id"] == self._waiting_on_outlinks_msg_id:
            self.logger.debug("got outlinks message=%s", message)
            self._outlinks = frozenset(message["result"]["result"]["value"].split(" "))
        elif message["id"] == self._waiting_on_document_url_msg_id:
            if message["result"]["result"]["value"] != self.url:
                if self.on_url_change:
                    self.on_url_change(message["result"]["result"]["value"])
            self._waiting_on_document_url_msg_id = None
        elif self._behavior and self._behavior.is_waiting_on_result(message["id"]):
            self._behavior.notify_of_result(message)

    def _handle_message(self, websock, message):
        # self.logger.debug("message from {} - {}".format(websock.url, message[:95]))
        # self.logger.debug("message from {} - {}".format(websock.url, message))
        message = json.loads(message)
        if "method" in message and message["method"] == "Network.requestWillBeSent":
            self._network_request_will_be_sent(message)
        elif "method" in message and message["method"] == "Network.responseReceived":
            self._network_response_received(message)
        elif "method" in message and message["method"] == "Page.loadEventFired":
            self._page_load_event_fired(message)
        elif "method" in message and message["method"] == "Console.messageAdded":
            self._console_message_added(message)
        elif "method" in message and message["method"] == "Debugger.paused":
            self._debugger_paused(message)
        elif "method" in message and message["method"] == "Inspector.targetCrashed":
            self._aw_snap_hes_dead_jim = message
        elif "result" in message:
            self._handle_result_message(message)
        # elif "method" in message and message["method"] in ("Network.dataReceived", "Network.responseReceived", "Network.loadingFinished"):
        #     pass
        # elif "method" in message:
        #     self.logger.debug("{} {}".format(message["method"], message))
        # else:
        #     self.logger.debug("[no-method] {}".format(message))

class Chrome:
    logger = logging.getLogger(__module__ + "." + __qualname__)

    def __init__(self, port, executable, user_home_dir, user_data_dir, proxy=None, ignore_cert_errors=False):
        self.port = port
        self.executable = executable
        self.user_home_dir = user_home_dir
        self.user_data_dir = user_data_dir
        self.proxy = proxy
        self.ignore_cert_errors = ignore_cert_errors
        self._shutdown = threading.Event()

    # returns websocket url to chrome window with about:blank loaded
    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.stop()

    # returns websocket url to chrome window with about:blank loaded
    def start(self):
        timeout_sec = 600
        new_env = os.environ.copy()
        new_env["HOME"] = self.user_home_dir
        chrome_args = [self.executable,
                "--use-mock-keychain", # mac thing
                "--user-data-dir={}".format(self.user_data_dir),
                "--remote-debugging-port={}".format(self.port),
                "--disable-web-sockets", "--disable-cache",
                "--window-size=1100,900", "--no-default-browser-check",
                "--disable-first-run-ui", "--no-first-run",
                "--homepage=about:blank", "--disable-direct-npapi-requests",
                "--disable-web-security"]
        if self.ignore_cert_errors:
            chrome_args.append("--ignore-certificate-errors")
        if self.proxy:
            chrome_args.append("--proxy-server={}".format(self.proxy))
        chrome_args.append("about:blank")
        self.logger.info("running: {}".format(" ".join(chrome_args)))
        self.chrome_process = subprocess.Popen(chrome_args, env=new_env,
                start_new_session=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0)
        self._out_reader_thread = threading.Thread(target=self._read_stderr_stdout,
                name="ChromeOutReaderThread(pid={})".format(self.chrome_process.pid))
        self._out_reader_thread.start()
        self.logger.info("chrome running, pid {}".format(self.chrome_process.pid))
        self._start = time.time()   # member variable just so that kill -QUIT reports it

        json_url = "http://localhost:%s/json" % self.port

        while True:
            try:
                raw_json = urllib.request.urlopen(json_url, timeout=30).read()
                all_debug_info = json.loads(raw_json.decode('utf-8'))
                debug_info = [x for x in all_debug_info if x['url'] == 'about:blank']

                if debug_info and 'webSocketDebuggerUrl' in debug_info[0]:
                    self.logger.debug("{} returned {}".format(json_url, raw_json))
                    url = debug_info[0]['webSocketDebuggerUrl']
                    self.logger.info('got chrome window websocket debug url {} from {}'.format(url, json_url))
                    return url
            except BaseException as e:
                if int(time.time() - self._start) % 10 == 5:
                    self.logger.warn("problem with %s (will keep trying until timeout of %d seconds): %s", json_url, timeout_sec, e)
                pass
            finally:
                if time.time() - self._start > timeout_sec:
                    self.logger.error("killing chrome, failed to retrieve %s after %s seconds", json_url, time.time() - self._start)
                    self.stop()
                    raise Exception("killed chrome, failed to retrieve {} after {} seconds".format(json_url, time.time() - self._start))
                else:
                    time.sleep(0.5)

    def _read_stderr_stdout(self):
        # XXX select doesn't work on windows
        def readline_nonblock(f):
            buf = b""
            while not self._shutdown.is_set() and (len(buf) == 0 or buf[-1] != 0xa) and select.select([f],[],[],0.5)[0]:
                buf += f.read(1)
            return buf

        try:
            while not self._shutdown.is_set():
                buf = readline_nonblock(self.chrome_process.stdout)
                if buf:
                    if re.search(b"Xlib:  extension|CERT_PKIXVerifyCert for [^ ]* failed|^ALSA lib|ERROR:gl_surface_glx.cc|ERROR:gpu_child_thread.cc", buf):
                        logging.debug("chrome pid %s STDERR %s", self.chrome_process.pid, buf)
                    else:
                        logging.warn("chrome pid %s STDERR %s", self.chrome_process.pid, buf)

                buf = readline_nonblock(self.chrome_process.stderr)
                if buf:
                    if re.search(b"Xlib:  extension|CERT_PKIXVerifyCert for [^ ]* failed|^ALSA lib|ERROR:gl_surface_glx.cc|ERROR:gpu_child_thread.cc", buf):
                        logging.debug("chrome pid %s STDERR %s", self.chrome_process.pid, buf)
                    else:
                        logging.warn("chrome pid %s STDERR %s", self.chrome_process.pid, buf)
        except:
            logging.error("unexpected exception", exc_info=True)

    def stop(self):
        if self._shutdown.is_set():
            return
        timeout_sec = 300
        self._shutdown.set()
        self.logger.info("terminating chrome pid {}".format(self.chrome_process.pid))

        self.chrome_process.terminate()
        first_sigterm = last_sigterm = time.time()

        try:
            while time.time() - first_sigterm < timeout_sec:
                time.sleep(0.5)

                status = self.chrome_process.poll()
                if status is not None:
                    if status == 0:
                        self.logger.info("chrome pid {} exited normally".format(self.chrome_process.pid, status))
                    else:
                        self.logger.warn("chrome pid {} exited with nonzero status {}".format(self.chrome_process.pid, status))
                    return

                # sometimes a hung chrome process will terminate on repeated sigterms
                if time.time() - last_sigterm > 10:
                    self.chrome_process.terminate()
                    last_sigterm = time.time()

            self.logger.warn("chrome pid {} still alive {} seconds after sending SIGTERM, sending SIGKILL".format(self.chrome_process.pid, timeout_sec))
            self.chrome_process.kill()
            status = self.chrome_process.wait()
            self.logger.warn("chrome pid {} reaped (status={}) after killing with SIGKILL".format(self.chrome_process.pid, status))
        finally:
            self._out_reader_thread.join()
            self.chrome_process = None

