"""
Internal objects for service implementation

Description:
  Internal objects used by the library to implement
  service capabilities

  Objects from this file should not be
  directly created by the user

Author:
  Itay Marom

"""

import simpy
from simpy.core import BoundClass
from ..trex_stl_exceptions import STLError
from ..trex_stl_psv import *
from scapy.layers.l2 import Ether
from .trex_stl_service import STLService

##################           
#
# STL service context
#
#
##################

class STLServiceCtx(object):
    '''
        service context provides the
        envoirment for running many services
        and their spawns in parallel
    '''
    def __init__ (self, client, port):
        self.client       = client
        self.port         = port
        self.port_obj     = client.ports[port]

######### API functions              #########

    def run (self, services):  
        '''
            Runs 'services' under service context
        '''
        with self.client.logger.supress():
            return self._run(services)
        
        
    def get_port_id (self):
        '''
            Returns the port ID attached to
            the context
        '''
        return self.port


    def get_src_ipv4 (self):
        '''
            Returns the source IPv4 of 
            the port under the context
            or None if the port is configured as L2
        '''
        layer_cfg = self.port_obj.get_layer_cfg()
        return layer_cfg['ipv4']['src'] if self.port_obj.is_l3_mode() else None



    def get_src_mac (self):
        '''
            returns the SRC mac of the port
            attached to the service
        '''

        layer_cfg = self.port_obj.get_layer_cfg()
        return layer_cfg['ether']['src']

######### internal functions              #########
        
    def _reset (self):
        self.filters    = {}
        self.services   = {}
        
        self.active_services = 0
     
             
    def _add (self, services):
        '''
            Add a service to the context
        '''
        if isinstance(services, STLService):
            self._add_single_service(services)

        elif isinstance(services, (list, tuple)) and all([isinstance(s, STLService) for s in services]):
            for service in services:
                self._add_single_service(service)

        else:
            raise STLError("'services' should be STLService subtype or list/tuple of it")


 
    def _run (self, services):
        
        # check port state
        self.client.psv.validate('SERVICE CTX', ports = self.port,
                                 states = (PSV_UP,
                                           PSV_ACQUIRED,
                                           PSV_SERVICE))
                
        # prepare
        self._reset()
        
        # add all services
        self._add(services)
        
        # create an enviorment
        self.env          = simpy.rt.RealtimeEnvironment(factor = 1, strict = False)
        self.tx_buffer    = TXBuffer(self.env, self.client, self.port)
        
        # create processes
        for service in self.services:
            pipe = self._pipe()
            self.services[service]['pipe'] = pipe
            p = self.env.process(service.run(pipe))
            self._on_process_create(p)
        
        # save promisicous state and move to enabled
        is_promiscuous = self.client.get_port_attr(port = self.port)['prom'] == "on"
        if not is_promiscuous:
            self.client.set_port_attr(ports = self.port, promiscuous = True)

        try:
            # for each filter, start a capture
            for f in self.filters.values():
                f['capture_id'] = self.client.start_capture(rx_ports = self.port, bpf_filter = f['inst'].get_bpf_filter())['id']

            # add the maintenace process
            tick_process = self.env.process(self._tick_process())

            # start the RT simulation - exit when the tick process dies
            self.env.run(until = tick_process)


        finally:
            # stop all captures
            for f in self.filters.values():
                if f['capture_id'] is not None:
                    self.client.stop_capture(f['capture_id'])

            if not is_promiscuous:
                self.client.set_port_attr(ports = self.port, promiscuous = False)
            self._reset()
            
 
    def _add_single_service (self, service):
        
        filter_type = service.get_filter_type()

        # if the service does not have a filter installed - create it
        if not filter_type in self.filters:
            self.filters[filter_type] = {'inst': filter_type(), 'capture_id': None}

        # add to the filter
        self.filters[filter_type]['inst'].add(service)

        # data per service
        self.services[service] = {'pipe': None}
        

    def _on_process_create (self, p):
        self.active_services += 1
        p.callbacks.append(self._on_process_exit)


    def _on_process_exit (self, event):
        self.active_services -= 1


    def _pipe (self):
        return STLServicePipe(self.env, self.tx_buffer)

        

    def _fetch_rx_pkts_per_filter (self, f):
        pkts = []
        self.client.fetch_capture_packets(f['capture_id'], pkts)

        # for each packet - try to forward to each service until we hit
        for pkt in pkts:
            scapy_pkt = Ether(pkt['binary'])
            rx_ts     = pkt['ts']
            
            # lookup all the services that this filter matches (usually 1)
            services = f['inst'].lookup(scapy_pkt)
            for service in services:
                self.services[service]['pipe']._on_rx_pkt(scapy_pkt, rx_ts)



    def _tick_process (self):
        
        while True:
            
            # if any packets are pending - send them
            self.tx_buffer.send_all()

            # poll for RX
            for f in self.filters.values():
                self._fetch_rx_pkts_per_filter(f)

            
            # if no other process exists - exit
            if self.active_services == 0:
                return
            else:
                # backoff
                yield self.env.timeout(0.05)

            
class TXBuffer(object):
    '''
        TX buffer
        handles buffering and sending packets
    '''
    def __init__ (self, env, client, port):
        self.env    = env
        self.client = client
        self.port   = port
        
        self.pkts     = []
        self.tx_event = self.env.event()
        
        
    def push (self, pkt):
        self.pkts.append(pkt)
        return self.tx_event
        
        
    def send_all (self):
        if self.pkts:
           rc = self.client.push_packets(ports = self.port, pkts = self.pkts, force = True)
           tx_ts = rc.data()['ts']

           self.pkts = []
           
           # mark as TX event happened
           self.tx_event.succeed(value = {'ts': tx_ts})
           # create a new event
           self.tx_event = self.env.event()
        

    def pending (self):
        return len(self.pkts)
        
        
class PktRX(simpy.resources.store.StoreGet):
    '''
        An event waiting for RX packets

        'limit' - the limit for the get event
                  None means unlimited
    '''
    def __init__ (self, store, timeout_sec = None, limit = None):
        self.limit = limit
        
        if timeout_sec is not None:
            self.timeout = store._env.timeout(timeout_sec)
            self.timeout.callbacks.append(self.on_get_timeout)
            
        super(PktRX, self).__init__(store)
        
        
    def on_get_timeout (self, event):
        '''
            Called when a timeout for RX packet has occured
            The event will be cancled (removed from queue)
            and a None value will be returend
        '''
        if not self.triggered:
            self.cancel()
            self.succeed([])

        

class Pkt(simpy.resources.store.Store):

    get = BoundClass(PktRX)

    def _do_get (self, event):
        if self.items:
            
            # if no limit - fetch all
            if event.limit is None:
                event.succeed(self.items)
                self.items = []
                
            else:
            # if limit was set - slice the list
                event.succeed(self.items[:event.limit])
                self.items = self.items[event.limit:]


##################
#
# STL service pipe
#
#
##################

class STLServicePipe(object):
    '''
        A pipe used to communicate between
        a service and the infrastructure
    '''

    def __init__ (self, env, tx_buffer):
        self.env         = env
        self.tx_buffer   = tx_buffer
        self.pkt         = Pkt(self.env)

        
    def async_wait (self, time_sec):
        '''
            Async wait for 'time_sec' seconds
        '''
        return self.env.timeout(time_sec)


    def async_wait_for_pkt (self, time_sec = None, limit = None):
        '''
            Wait for packet arrival for 'time_sec'

            if 'time_sec' is None will wait infinitly.
            if 'time_sec' is zero it will return immeaditly.

            if 'limit' is a number, it will return up to 'limit' packets
            even if there are more
            
            returns:
                list of packets
                each packet is a dict:
                    'pkt' - scapy packet
                    'ts'  - arrival TS (server time)
                    
        '''
        return self.pkt.get(time_sec, limit)


    def async_tx_pkt (self, tx_pkt):
        '''
            Called by the sender side
            to transmit a packet
            
            'tx_pkt' - pkt as a binary to send
            
            call can choose to yield for TX actual
            event or ignore

            returns:
                dict:
                    'ts' - TX timestamp (server time)
        '''
        return self.tx_buffer.push(tx_pkt)

        
################### internal functions ##########################

    def _on_rx_pkt (self, pkt, rx_ts):
        '''
            Called by the reciver side
            (the service)
        '''
        self.pkt.put({'pkt': pkt, 'ts': rx_ts})

