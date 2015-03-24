import json
import time
import urlparse

import cyclone.web

from boto.dynamodb2.exceptions import (
    ProvisionedThroughputExceededException,
)
from cryptography.fernet import InvalidToken
from twisted.internet.threads import deferToThread
from twisted.python import log


class EndpointHandler(cyclone.web.RequestHandler):

    def addCors(self):
        if self.settings.cors:
            self.set_header("Access-Control-Request-Method", "*")

    @cyclone.web.asynchronous
    def options(self, token):
        self.addCors()
        self.set_status(200)
        self.write("")
        self.finish()

    @cyclone.web.asynchronous
    def head(self, token):
        self.addCors()
        self.set_status(200)
        self.write("")
        self.finish()

    @cyclone.web.asynchronous
    def default(self, token):
        self.addCors()
        self.set_status(521)
        self.finish()

    @cyclone.web.asynchronous
    def put(self, token):
        self.metrics = self.settings.metrics
        self.start_time = time.time()
        fernet = self.settings.fernet

        self.blah = "application/x-www-form-urlencoded"
        self.addCors()

        # If there's a request body, parse it out
        version = data = None
        if len(self.request.body) > 0:
            body_args = urlparse.parse_qs(self.request.body,
                                          keep_blank_values=True)
            version = body_args.get("version")
            data = body_args.get("data")
        else:
            version = self.request.arguments.get("version")
            data = self.request.arguments.get("data")

        # These come out as lists, unlist them
        if version is not None:
            version = version[0]
        if data is not None:
            data = data[0]

        # Blank version is ok, None means not even blank though
        if version is None:
            self.set_status(401)
            self.write("No version present")
            return self.finish()

        if data and len(data) > self.settings.max_data:
            self.set_status(401)
            self.write("Data too large")
            return self.finish()

        self.version, self.data = version, data

        d = deferToThread(fernet.decrypt, token.encode('utf8'))
        d.addCallback(self._process_token)
        d.addErrback(self._bad_token).addErrback(self._error_response)

    def _process_token(self, result):
        uaid, chid = result.split(":")
        if not self.version:
            self.version = int(time.time())

        self.uaid, self.chid = uaid, chid

        d = deferToThread(self.settings.router.get_uaid, uaid)
        d.addCallback(self._process_uaid)
        d.addErrback(self._handle_overload).addErrback(self._error_response)

    def _bad_token(self, failure):
        failure.trap(InvalidToken)
        self.set_status(401)
        self.write("Invalid token")
        self.finish()

    def _handle_overload(self, failure):
        failure.trap(ProvisionedThroughputExceededException)
        self.set_status(503)
        self.write("Server busy, try later")
        self.finish()

    def _error_response(self, failure):
        log.err(failure)
        self.set_status(500)
        self.write("Error processing request")
        self.finish()

    def _process_uaid(self, result):
        """Process the result of the AWS call"""
        if not result:
            self.set_status(404)
            self.write("Invalid")
            return self.finish()

        # Determine if they're connected at the moment
        node_id = result.get("node_id")

        # Indicator if we got a node_id, but the node won't handle
        # delivery at this moment later.
        self.client_check = False

        if node_id:
            # Attempt a delivery if they are connected
            payload = json.dumps([{"channelID": self.chid,
                                   "version": int(self.version),
                                   "data": self.data}])
            d = deferToThread(
                self.settings.requests.put,
                node_id + "/push/" + self.uaid,
                data=payload
            )
            d.addCallback(self._process_routing, result)
            d.addErrback(self._error_response)
        else:
            self._save_notification()

    def _process_routing(self, result, item):
        if result.status_code == 200:
            # Success, return!
            self.metrics.increment("router.broadcast.hit")
            time_diff = time.time() - self.start_time
            self.metrics.timing("updates.handled", duration=time_diff)
            self.write("Success")
            return self.finish()
        elif result.status_code == 404:
            node_id = item.get("node_id")
            # Conditionally delete the node_id
            d = deferToThread(self.settings.router.clear_node, item)
            d.addCallback(self._process_node_delete, node_id)
            d.addErrback(self._handle_overload).addErrback(self._error_response)
            return

        # Client was busy, remember to tell it to check
        self.client_check = result.status_code == 503
        self._save_notification()

    def _process_node_delete(self, result, node_id):
        if not result:
            # Client hopped, punt this request so app-server can
            # try again and get luckier
            self.set_status(503)
            self.write("Server is busy")
            self.finish()
        else:
            # Delete was ok, proceed to save the notification
            self._save_notification(node_id)

    def _save_notification(self, node_id=None):
        """Save the notification"""
        d = deferToThread(self.settings.storage.save_notification,
                          uaid=self.uaid, chid=self.chid, version=self.version)
        d.addCallback(self._process_save, node_id)
        d.addErrback(self._handle_overload).addErrback(self._error_response)

    def _process_save(self, result, node_id=None):
        if self.client_check:
            # If we already know where the client was connected...
            d = deferToThread(self.settings.requests.put,
                              node_id + "/notif/" + self.uaid)
            d.addCallback(self._process_notif, node_id)
            d.addErrback(self._error_response)
        else:
            # Saved the notification, check for if the client is somewhere
            # now
            d = deferToThread(self.settings.router.get_uaid, self.uaid)
            d.addCallback(self._process_jumped_client)
            d.addErrback(self._error_response)

    def _process_notif(self, result, node_id=None):
        """Process the result of a requests.PUT to a Connection Node's
        /notif/ handler"""
        if result.status_code != 404:
            # Client was notified fine, we're done
            self._finish_missed_store()
            return

        # Client jumped, if they reconnected somewhere, try one more time
        d = deferToThread(self.settings.router.get_uaid, self.uaid)
        d.addCallback(self._process_jumped_client)
        d.addErrback(self._handle_overload).addErrback(self._error_response)

    def _process_jumped_client(self, result):
        if not result:
            # Client got deleted too? bummer.
            self.set_status(404)
            self.write("Invalid")
            return self.finish()

        node_id = result.get("node_id")
        d = deferToThread(self.settings.requests.put,
                          node_id + "/notif/" + self.uaid)
        # No check on response here, because if they jumped since we
        # got this they'll definitely get the stored notification
        # We ignore errors here too, as that's a hell of an edge case
        d.addBoth(self._finish_missed_store)

    def _finish_missed_store(self, result=None):
        self.metrics.increment("router.broadcast.miss")
        self.write("Success")
        self.finish()

    def write_error(self, code, exception=None):
        """ Write the error (otherwise unhandled exception) """
        reason = "No reason"
        if exception is not None and exception.reason is not None:
            reason = exception.reason
        error = "%d: %s" % (code, reason)
        self.set_status(code)
        log.err("Endpoint write_error: %s" % error)
        self.finish()
