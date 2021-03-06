#!/usr/bin/python3 -u
from http.server import HTTPServer, BaseHTTPRequestHandler
from http.client import BAD_REQUEST, INTERNAL_SERVER_ERROR, NOT_IMPLEMENTED
from http.client import IncompleteRead
from socketserver import ForkingMixIn
import os
import select
import socket
from tempfile import TemporaryFile
import cups
from io import BytesIO
from gi.repository import printerd
from gi.repository import GLib
from gi.repository import Gio

cups.require ("1.9.70")

if os.getuid() == 0:
    server_address = ('', 631)
else:
    server_address = ('', 8631)

class ObjectAddress:
    """
    Base class for manipulating object addresses.
    """

    DBUS_PATH_PREFIX = ""
    URI_FORMAT = ""
    ID_TYPE = str

    def __init__ (self, uri=None, path=None, id=None):
        if uri:
            self._id = self.ID_TYPE (uri[uri.rfind ("/") + 1:])
        elif path:
            if not path.startswith (self.DBUS_PATH_PREFIX):
                raise RuntimeError ("Invalid path")

            self._id = self.ID_TYPE (path[len (self.DBUS_PATH_PREFIX):])
        elif id:
            self._id = self.ID_TYPE (id)
        else:
            assert ((uri != None and path == None and id == None) or
                    (path != None and uri == None and id == None) or
                    (id != None and uri == None and path == None))

    def get_uri (self):
        return self.URI_FORMAT % (server_address[0],
                                  server_address[1],
                                  self._id)

    def get_path (self):
        return "%s%s" % (self.DBUS_PATH_PREFIX, self._id)

    def get_id (self):
        return self._id

class PrinterAddress(ObjectAddress):
    DBUS_PATH_PREFIX = "/org/freedesktop/printerd/printer/"
    URI_FORMAT = "ipp://%s:%s/printers/%s"

class JobAddress(ObjectAddress):
    DBUS_PATH_PREFIX = "/org/freedesktop/printerd/job/"
    URI_FORMAT = "ipp://%s:%s/jobs/%s"
    ID_TYPE = int

class Attributes(dict):
    """
    IPP attributes as a dict.
    """

    def __init__ (self, attributes):
        super ().__init__ ()
        for attribute in attributes:
            self[attribute.name] = attribute

    def get_value (self, k, n=0, d=None):
        """
        Return the named and indexed attribute value if it exists,
        otherwise return default.
        """
        try:
            return self[k].values[n]
        except (KeyError, IndexError):
            return d

class PdClient:
    """
    Class for getting objects using printerd.Client.
    """

    IFACE_PRINTERD_PREFIX = "org.freedesktop.printerd"
    IFACE_MANAGER = IFACE_PRINTERD_PREFIX + ".Manager"
    IFACE_DEVICE  = IFACE_PRINTERD_PREFIX + ".Device"
    IFACE_PRINTER = IFACE_PRINTERD_PREFIX + ".Printer"
    IFACE_JOB     = IFACE_PRINTERD_PREFIX + ".Job"

    def __init__ (self):
        self.client = printerd.Client.new_sync ()
        self.object_manager = self.client.get_object_manager ()

    def get_manager (self):
        return self.client.get_manager ()

    def get_printer (self, objpath):
        return self.object_manager.\
            get_object (objpath).\
            get_interface (self.IFACE_PRINTER)

    def get_job (self, objpath):
        return self.object_manager.\
            get_object (objpath).\
            get_interface (self.IFACE_JOB)

class IPPServer(BaseHTTPRequestHandler):
    """
    Base class implementing common IPP parts.
    """

    IPP_METHODS = {}

    protocol_version = "HTTP/1.1"

    def read_specified (self, length):
        data = []
        while length > 0:
            part = self.rfile.read (length)
            if not part:
                raise IncompleteRead (b''.join (data), length)
            data.append (part)
            length -= len (part)

        return b''.join (data)

    def read_chunk_size (self):
        line = self.rfile.readline ()
        ext = line.find (b';')
        if ext != -1:
            line = line[:ext]

        return int (line, base=16)

    def read_chunk (self, chunk_size):
        data = self.read_specified (chunk_size)

        # Discard CRLF
        self.read_specified (2)

        return data

    def read_all_chunks (self):
        data = []
        chunk_size = None
        while True:
            chunk_size = self.read_chunk_size ()
            if chunk_size == 0:
                break

            data.append (self.read_chunk (chunk_size))

        # read and discard trailer
        return b''.join (data)

    def do_POST (self):
        if self.headers.get ('content-type') != "application/ipp":
            self.send_error (BAD_REQUEST, "Bad content type")
            return

        transfer_encoding = self.headers.get ('transfer-encoding')
        chunked = (transfer_encoding and
                   transfer_encoding.lower () == 'chunked')

        if not chunked:
            try:
                length = int (self.headers['content-length'])
                if length < 0:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                self.send_error (BAD_REQUEST, "Content-Length not specified")
                return

            try:
                data = self.read_specified (length)
            except IncompleteRead as e:
                self.send_error (BAD_REQUEST, e.message)
                return
        else:
            # Read all chunks.
            try:
                data = self.read_all_chunks ()
            except IncompleteRead as e:
                self.send_error (BAD_REQUEST, e.message)
                return
            except ValueError:
                self.send_error (BAD_REQUEST)
                return

        req = cups.IPPRequest ()
        bytes = BytesIO (data)
        try:
            status = req.readIO (bytes.read)
        except:
            self.send_error (BAD_REQUEST)
            return

        if status == cups.IPP_STATE_ERROR:
            self.send_error (BAD_REQUEST)
            return

        self.ipprequest = req
        self.request_file = bytes
        op = req.operation
        self.log_message ("%s: %r" % (cups.ippOpString (op),
                                      req.attributes))
        try:
            method_name = self.IPP_METHODS[op]
            method = getattr (self, method_name)
        except (KeyError, AttributeError):
            # Not implemented
            self.send_error (NOT_IMPLEMENTED)
            return

        try:
            method ()
        except:
            # Internal error
            self.send_error (INTERNAL_SERVER_ERROR)
            raise

        # Send response
        self.wfile.flush ()

    def get_printerd (self):
        self.printerd = PdClient ()
        return self.printerd

    def send_ipp_response (self, req):
        req.state = cups.IPP_STATE_IDLE
        self.log_message ("Response: %s %r" %
                          (cups.ippErrorString (req.statuscode),
                           req.attributes))
        outstream = BytesIO ()
        req.writeIO (outstream.write)
        output = outstream.getvalue ()
        self.send_response (200)
        self.send_header ("Content-Type", "application/ipp")
        self.send_header ("Content-Length", str (len (output)))
        self.end_headers ()
        self.wfile.write (output)

    def send_ipp_statuscode (self, statuscode, message=None):
        req = self.ipprequest
        req.statuscode = statuscode
        if message:
            req.add(cups.IPPAttribute (cups.IPP_TAG_OPERATION,
                                       cups.IPP_TAG_TEXT,
                                       "status-message",
                                       message))

        self.send_ipp_response (req)

class PdIPPServer(IPPServer):
    """
    Class providing implementations of IPP operations.
    """

    IPP_METHODS = {
        cups.IPP_OP_CUPS_GET_PRINTERS:  "ipp_CUPS_Get_Printers",
        cups.IPP_OP_CREATE_JOB:         "ipp_Create_Job",
        cups.IPP_OP_SEND_DOCUMENT:      "ipp_Send_Document",
        cups.IPP_OP_CANCEL_JOB:         "ipp_Cancel_Job",
    }

    def ipp_CUPS_Get_Printers (self):
        req = self.ipprequest
        manager = self.get_printerd ().get_manager ()
        printers = manager.call_get_printers_sync ()
        if len (printers) > 0:
            req.statuscode = cups.IPP_STATUS_OK
        else:
            req.statuscode = cups.IPP_STATUS_ERROR_NOT_FOUND

        first = True
        for objpath in printers:
            if not first:
                req.addSeparator ()

            first = False
            printer = self.printerd.get_printer (objpath)

            name = PrinterAddress (path=objpath).get_id ()
            req.add (cups.IPPAttribute (cups.IPP_TAG_PRINTER,
                                        cups.IPP_TAG_NAME,
                                        "printer-name",
                                        name))
            req.add (cups.IPPAttribute (cups.IPP_TAG_PRINTER,
                                        cups.IPP_TAG_URI,
                                        "device-uri",
                                        printer.props.device_uris[0]))

        self.send_ipp_response (req)

    def ipp_Create_Job (self):
        attrs = Attributes (self.ipprequest.attributes)
        uri = attrs.get_value ('printer-uri')
        if not uri:
            self.send_error (400, "No printer-uri attribute")
            return

        objpath = PrinterAddress (uri=uri).get_path ()
        self.get_printerd ()
        try:
            printer = self.printerd.get_printer (objpath)
        except AttributeError:
            self.send_ipp_statuscode (cups.IPP_STATUS_ERROR_NOT_FOUND,
                                      "Specified printer does not exist")
            return

        self.log_message ("printer-uri: %s" % attrs.get ('printer-uri'))
        options = GLib.Variant ("a{sv}", {})
        name = attrs.get_value ('job-name', d='')
        jobattrs = GLib.Variant ("a{sv}", {})
        try:
            jobpath, unsupported = printer.call_create_job_sync (options,
                                                                 name,
                                                                 jobattrs,
                                                                 None)
        except GLib.GError as e:
            self.send_ipp_statuscode (cups.IPP_STATUS_ERROR_NOT_POSSIBLE,
                                      e.message)
            return

        req = cups.IPPRequest ()
        jobid = JobAddress (path=jobpath).get_id ()
        req.add (cups.IPPAttribute (cups.IPP_TAG_JOB,
                                    cups.IPP_TAG_INTEGER,
                                    "job-id",
                                    jobid))
        self.send_ipp_response (req)

    def ipp_Send_Document (self):
        attrs = Attributes (self.ipprequest.attributes)
        jobid = attrs.get_value ('job-id')
        if jobid:
            jobpath = JobAddress (id=jobid).get_path ()
        else:
            uri = attrs.get_value ('job-uri')
            if uri:
                jobpath = JobAddress (uri=uri).get_path ()
            else:
                self.send_error (BAD_REQUEST, "No job-id or job-uri attribute")
                return

        self.get_printerd ()
        try:
            job = self.printerd.get_job (jobpath)
        except AttributeError:
            self.send_ipp_statuscode (cups.IPP_STATUS_ERROR_NOT_FOUND,
                                      "Specified job does not exist")
            return

        with TemporaryFile (prefix='ippd') as tmpfile:
            tmpfile.write (self.request_file.read ())
            tmpfile.seek (0)
            options = GLib.Variant ("a{sv}", {})
            file_descriptor = GLib.Variant ("h", 0)
            fd_list = Gio.UnixFDList.new_from_array ([tmpfile.fileno ()])
            try:
                job.call_add_document_sync (options, file_descriptor,
                                            fd_list, None)
            except GLib.Error as e:
                self.send_ipp_statuscode (cups.IPP_STATUS_ERROR_NOT_POSSIBLE,
                                          e.message)
                return

        if attrs.get ('last-document', True):
            options = GLib.Variant ("a{sv}", {})
            try:
                job.call_start_sync (options, None)
            except GLib.Error as e:
                self.send_ipp_statuscode (cups.IPP_STATUS_ERROR_NOT_POSSIBLE,
                                          e.message)
                return

        self.send_ipp_statuscode (cups.IPP_STATUS_OK)

    def ipp_Cancel_Job (self):
        attrs = Attributes (self.ipprequest.attributes)
        jobid = attrs.get_value ('job-id')
        if jobid:
            jobpath = JobAddress (id=jobid).get_path ()
        else:
            uri = attrs.get_value ('job-uri')
            if uri:
                jobpath = JobAddress (uri=uri).get_path ()
            else:
                self.send_error (BAD_REQUEST, "No job-id or job-uri attribute")
                return

        self.get_printerd ()
        try:
            job = self.printerd.get_job (jobpath)
        except AttributeError:
            self.send_ipp_statuscode (cups.IPP_STATUS_ERROR_NOT_FOUND,
                                      "Specified job does not exist")
            return

        options = GLib.Variant ("a{sv}", {})
        try:
            job.call_cancel_sync (options, None)
        except GLib.Error as e:
            self.send_ipp_statuscode (cups.IPP_STATUS_ERROR_NOT_POSSIBLE,
                                      e.message)
            return

        self.send_ipp_statuscode (cups.IPP_STATUS_OK)

class ForkingHTTPServer(ForkingMixIn, HTTPServer):
    pass

class SocketInheritingIPPServer(ForkingHTTPServer):
    """
    An IPPServer subclass that takes over an inherited socket from
    systemd.
    """
    def __init__ (self, address_info, handler, fd, bind_and_activate=True):
        super ().__init__ (address_info, handler, bind_and_activate=False)
        self.socket = socket.fromfd (fd, self.address_family, self.socket_type)
        if bind_and_activate:
            # Only activate, as systemd provides ready-bound sockets.
            self.server_activate ()

if os.environ.get ('LISTEN_PID') == str (os.getpid ()):
    SYSTEMD_FIRST_SOCKET_FD = 3
    ippd = SocketInheritingIPPServer (server_address, PdIPPServer,
                                      fd=SYSTEMD_FIRST_SOCKET_FD)
else:
    ippd = ForkingHTTPServer (server_address, PdIPPServer)

if __name__ == '__main__':
    try:
        ippd.serve_forever ()
    except KeyboardInterrupt:
        pass
