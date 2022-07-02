# TinyTuya Setup Wizard
# -*- coding: utf-8 -*-
"""
TinyTuya API Server for Tuya based WiFi smart devices

Author: Jason A. Cox
Date: June 11, 2022
For more information see https://github.com/jasonacox/tinytuya

Description
    Server continually listens for Tuya UDP discovery packets and updates the database of devices
    and uses devices.json to determine metadata about devices. 
    Server listens for GET requests on local port 8888:
        /devices                                - List all devices discovered with metadata   
        /device/{DeviceID}                      - List specific device metadata
        /numdevices                             - List current number of devices discovered
        /status/{DeviceID}                      - List current device status
        /set/{DeviceID}/{Key}/{Value}           - Set DPS {Key} with {Value} 
        /turnon/{DeviceID}/{SwitchNo}           - Turn on device, optional {SwtichNo}
        /turnoff/{DeviceID}/{SwitchNo}          - Turn off device, optional {SwtichNo}
        /sync                                   - Fetches the device list and local keys from the Tuya Cloud API
        /cloudconfig/{apiKey}/{apiSecret}/{apiRegion}/{apiDeviceID}   - Sets the Tuya Cloud API login info

"""

# Modules
from __future__ import print_function
import threading
import time
import logging
import json
import socket
import requests
import resource
import sys
import os
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from socketserver import ThreadingMixIn 

# Required module: pycryptodome
try:
    import Crypto
    from Crypto.Cipher import AES  # PyCrypto
except ImportError:
    Crypto = AES = None
    import pyaes  # https://github.com/ricmoo/pyaes

# Terminal color capability for all platforms
try:
    from colorama import init
    init()
except:
    pass

import tinytuya

BUILD = "t1"

# Defaults
APIPORT = 8888
DEBUGMODE = False
DEVICEFILE = tinytuya.DEVICEFILE
SNAPSHOTFILE = tinytuya.SNAPSHOTFILE
TCPTIMEOUT = tinytuya.TCPTIMEOUT    # Seconds to wait for socket open for scanning
TCPPORT = tinytuya.TCPPORT          # Tuya TCP Local Port
MAXCOUNT = tinytuya.MAXCOUNT        # How many tries before stopping
UDPPORT = tinytuya.UDPPORT          # Tuya 3.1 UDP Port
UDPPORTS = tinytuya.UDPPORTS        # Tuya 3.3 encrypted UDP Port
TIMEOUT = tinytuya.TIMEOUT          # Socket Timeout
RETRYTIME = 30
RETRYCOUNT = 5
SAVEDEVICEFILE = True

# Static Assets
web_root = os.path.join(os.path.dirname(__file__), "web")

# Logging
log = logging.getLogger(__name__)

# Global Stats
serverstats = {}
serverstats['tinytuya'] = "%s%s" % (tinytuya.version, BUILD)
serverstats['gets'] = 0
serverstats['errors'] = 0
serverstats['timeout'] = 0
serverstats['api'] = {}
serverstats['ts'] = int(time.time())         # Timestamp for Now
serverstats['start'] = int(time.time())      # Timestamp for Start 

# Global Variables
running = True
tuyadevices = []
deviceslist = {}
newdevices = []
retrydevices = {}
retrytimer = 0
cloudconfig = {'apiKey':'', 'apiSecret':'', 'apiRegion':'', 'apiDeviceID':''}


# Terminal formatting
(bold, subbold, normal, dim, alert, alertdim, cyan, red, yellow) = tinytuya.termcolor(True)

# Helpful Functions

def tuyaLookup(deviceid):
    #  Function to Lookup Tuya device info by (id) returning (name, key)
    for i in tuyadevices:
        if i["id"] == deviceid:
            if "mac" in i:
                return (i["name"], i["key"], i["mac"])
            else:
                return (i["name"], i["key"], "")
    return ("", "", "")

def appenddevice(newdevice, devices):
    if newdevice["id"] in devices:
        return True
    """
    for i in devices:
        if i['ip'] == newdevice['ip']:
                return True
    """
    devices[newdevice["id"]] = newdevice
    return False

def formatreturn(value):
    if value is None:
        result = {"status": "OK"}
    elif type(value) is dict:
        result = value
    else:
        result = {"status": value}
    return(json.dumps(result))

def get_static(web_root, fpath):
    if fpath.split('?')[0] == "/":
        fpath = "index.html"
    if fpath.startswith("/"):
        fpath = fpath[1:]
    fpath = fpath.split("?")[0]
    freq = os.path.join(web_root, fpath)
    if os.path.exists(freq):
        if freq.lower().endswith(".js"):
            ftype = "application/javascript"
        elif freq.lower().endswith(".css"):
            ftype = "text/css"
        elif freq.lower().endswith(".png"):
            ftype = "image/png"
        elif freq.lower().endswith(".html"):
            ftype = "text/html"
        else:
            ftype = "text/plain"

        with open(freq, 'rb') as f:
            return f.read(), ftype

    return None, None

def tuyaLoadJson():
    # Check to see if we have additional Device info
    tdevices = []
    try:
        # Load defaults
        with open(DEVICEFILE) as f:
            tdevices = json.load(f)
        log.debug("loaded=%s [%d devices]", DEVICEFILE, len(tdevices))
    except:
        # No Device info
        log.debug("Device file %s could not be loaded", DEVICEFILE)

    return tdevices

def tuyaSaveJson():
    if not SAVEDEVICEFILE:
        return False

    try:
        with open(DEVICEFILE, 'w') as f:
            json.dump(tuyadevices, f, indent=4)
        log.debug("saved=%s [%d devices]", DEVICEFILE, len(tuyadevices))
    except:
        return False

    return True

tuyadevices = tuyaLoadJson()

# Debug Mode
tinytuya.set_debug(DEBUGMODE)

def tuyaCloudRefresh():
    log.debug("Calling Cloud Refresh")
    if cloudconfig['apiKey'] == '' or cloudconfig['apiSecret'] == '' or cloudconfig['apiRegion'] == '' or cloudconfig['apiDeviceID'] == '':
        log.debug("Cloud API config missing, not loading")
        return {'Error': 'Cloud API config missing'}

    global tuyadevices
    cloud = tinytuya.Cloud( **cloudconfig )
    # on auth error, cloud.token is a dict and will cause getdevices() to implode
    if isinstance( cloud.token, dict):
        return cloud.token
    tuyadevices = cloud.getdevices(False)
    tuyaSaveJson()
    return {'devices': tuyadevices}

# Threads
def tuyalisten(port):
    """
    Thread to listen for Tuya devices UDP broadcast on port 
    """
    log.debug("Started tuyalisten thread on %d", port)

    # Enable UDP listening broadcasting mode on UDP port 
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    client.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    client.bind(("", port))
    client.settimeout(5)

    while(running):
        try:
            data, addr = client.recvfrom(4048)
        except (KeyboardInterrupt, SystemExit) as err:
            break
        except Exception as err:
            continue
        ip = addr[0]
        gwId = dname = dkey = mac = ""
        result = data
        try:
            result = data[20:-8]
            try:
                result = tinytuya.decrypt_udp(result)
            except:
                result = result.decode()
            result = json.loads(result)
            #log.debug("Received valid UDP packet: %r", result)
            ip = result["ip"]
            gwId = result["gwId"]
        except:
            result = {"ip": ip}
            #log.debug("Invalid UDP Packet: %r", result)
        try:
            # Try to pull name and key data
            (dname, dkey, mac) = tuyaLookup(gwId)
        except:
            pass
        # set values
        result["name"] = dname
        result["mac"] = mac
        result["key"] = dkey
        result["id"] = gwId

        # add device if new
        if not appenddevice(result, deviceslist):
            # Added device to list
            if dname == "" and dkey == "" and result["id"] not in newdevices:
                # If fetching the key failed, save it to retry later
                retrydevices[result["id"]] = RETRYCOUNT
                newdevices.append(result["id"])
    print('tuyalisten', port, 'Exit')

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    pass

class handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        if DEBUGMODE:
            sys.stderr.write("%s - - [%s] %s\n" %
                         (self.address_string(),
                          self.log_date_time_string(),
                          format%args))
        else:
            pass

    def address_string(self):
        # replace function to avoid lookup delays
        host, hostport = self.client_address[:2]
        return host

    def do_POST(self):
        self.send_response(200)
        message = "Error"
        contenttype = 'application/json'
        # Send headers and payload  
        self.send_header('Content-type',contenttype)
        self.send_header('Content-Length', str(len(message)))
        self.end_headers()
        self.wfile.write(bytes(message, "utf8"))

    def do_GET(self):
        global retrytimer, retrydevices
        global cloudconfig, deviceslist

        self.send_response(200)
        message = "Error"
        contenttype = 'application/json'
        if self.path == '/devices':
            message = json.dumps(deviceslist)
        elif self.path == '/stats':
            # Give Internal Stats
            serverstats['ts'] = int(time.time())
            serverstats['mem'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            message = json.dumps(serverstats)
        elif self.path.startswith('/set/'):
            try:
                # ['', 'set', 'deviceid', 'key', 'value']
                (ignore1, ignore2, id, dpsKey, dpsValue) = self.path.split('/')
                # convert to correct types
                if dpsValue.lower() == "true":
                    dpsValue = True
                if dpsValue.lower() == "false":
                    dpsValue = False
                if dpsValue.isnumeric():
                    dpsValue = int(dpsValue)
            except:
                message = json.dumps({"Error": "Syntax error in set command URL.", "url": self.path})
            if(id in deviceslist):
                d = tinytuya.OutletDevice(id, deviceslist[id]["ip"], deviceslist[id]["key"])
                d.set_version(float(deviceslist[id]["version"]))
                message = formatreturn(d.set_value(dpsKey,dpsValue,nowait=True))
                d.close()
            else:
                message = json.dumps({"Error": "Device ID not found.", "id": id})
        elif self.path.startswith('/device/'):
            id = self.path.split('/device/')[1]
            if(id in deviceslist):
                message = json.dumps(deviceslist[id])
            else:
                message = json.dumps({"Error": "Device ID not found.", "id": id})
        elif self.path.startswith('/turnoff/'):
            id = self.path.split('/turnoff/')[1]
            sw = 1
            if "/" in id:
                try:
                    (id, sw) = id.split("/")
                except:
                    id = ""
                    message = json.dumps({"Error": "Invalid syntax in turnoff command.", "url": self.path})
            if id in deviceslist:
                try:
                    d = tinytuya.OutletDevice(id, deviceslist[id]["ip"], deviceslist[id]["key"])
                    d.set_version(float(deviceslist[id]["version"]))
                    message = formatreturn(d.turn_off(switch=sw, nowait=True))
                    d.close()
                except:
                    message = json.dumps({"Error": "Error sending command to device.", "id": id})
            elif id != "":
                message = json.dumps({"Error": "Device ID not found.", "id": id})            
        elif self.path.startswith('/turnon/'):
            id = self.path.split('/turnon/')[1]
            sw = 1
            if "/" in id:
                try:
                    (id, sw) = id.split("/")
                except:
                    id = ""
                    message = json.dumps({"Error": "Invalid syntax in turnon command.", "url": self.path})
            if id in deviceslist:
                try:
                    d = tinytuya.OutletDevice(id, deviceslist[id]["ip"], deviceslist[id]["key"])
                    d.set_version(float(deviceslist[id]["version"]))
                    message = formatreturn(d.turn_on(switch=sw, nowait=True))
                    d.close()
                except:
                    message = json.dumps({"Error": "Error sending command to device.", "id": id})
            elif id != "":
                message = json.dumps({"Error": "Device ID not found.", "id": id})             
        elif self.path == '/numdevices':
            jout = {}
            jout["found"] = len(deviceslist)
            jout["registered"] = len(tuyadevices)
            message = json.dumps(jout)
        elif self.path.startswith('/status/'):
            id = self.path.split('/status/')[1]
            if(id in deviceslist):
                try:
                    d = tinytuya.OutletDevice(id, deviceslist[id]["ip"], deviceslist[id]["key"])
                    d.set_version(float(deviceslist[id]["version"]))
                    message = formatreturn(d.status())
                    d.close()
                except:
                    message = json.dumps({"Error": "Error polling device.", "id": id})
            else:
                message = json.dumps({"Error": "Device ID not found.", "id": id})
        elif self.path == '/sync':
            message = json.dumps(tuyaCloudRefresh())
            retrytimer = time.time() + RETRYTIME
            retrydevices['*'] = 1
        elif self.path.startswith('/cloudconfig/'):
            cfgstr = self.path.split('/cloudconfig/')[1]
            cfg = cfgstr.split('/')
            if len(cfg) != 4:
                message = json.dumps({"Error": "Syntax error in cloud config command URL."})
            else:
                cloudconfig['apiKey'] = cfg[0]
                cloudconfig['apiSecret'] = cfg[1]
                cloudconfig['apiRegion'] = cfg[2]
                cloudconfig['apiDeviceID'] = cfg[3]
                message = json.dumps(tuyaCloudRefresh())
                retrytimer = time.time() + RETRYTIME
                retrydevices['*'] = 1
        else:
            # Serve static assets from web root first, if found.
            fcontent, ftype = get_static(web_root, self.path)
            if fcontent:
                self.send_header('Content-type','{}'.format(ftype))
                self.send_header('Content-Length', str(len(fcontent)))
                self.end_headers()
                self.wfile.write(fcontent)
                return

        # Counts 
        if "Error" in message:
            serverstats['errors'] = serverstats['errors'] + 1
        serverstats['gets'] = serverstats['gets'] + 1

        # Send headers and payload
        self.send_header('Content-type',contenttype)
        self.send_header('Content-Length', str(len(message)))
        self.end_headers()
        self.wfile.write(bytes(message, "utf8"))

def api(port):
    """
    API Server - Thread to listen for commands on port 
    """
    log.debug("Started API server thread on %d", port)

    with ThreadingHTTPServer(('', port), handler) as server:
        try:
            # server.serve_forever()
            while running:
                server.handle_request()
        except:
            print(' CANCEL \n')
    print('api Exit')

# MAIN Thread
if __name__ == "__main__":
    # creating thread
    tuyaUDP = threading.Thread(target=tuyalisten, args=(UDPPORT,))
    tuyaUDPs = threading.Thread(target=tuyalisten, args=(UDPPORTS,))
    apiServer = threading.Thread(target=api, args=(APIPORT,))
    
    print(
        "\n%sTinyTuya %s(Server)%s [%s%s]\n"
        % (bold, normal, dim, tinytuya.__version__, BUILD)
    )
    if len(tuyadevices) > 0:
        print("%s[Loaded devices.json - %d devices]%s\n" % (dim, len(tuyadevices), normal))
    else:
        print("%sWARNING: No devices.json found - limited functionality.%s\n" % (alertdim,normal))

    # start threads
    print("Starting threads...")
    tuyaUDP.start()
    tuyaUDPs.start()
    apiServer.start()

    print(" - API and UI Endpoint on http://localhost:%d" % APIPORT)
    
    try:
        while(True):
            print("\r - Discovered Devices: %d   " % len(deviceslist), end='')

            if retrytimer <= time.time() or '*' in retrydevices:
                if len(retrydevices) > 0:
                    # only refresh the cloud if we are not here because /sync was called
                    if '*' not in retrydevices:
                        tuyaCloudRefresh()
                        retrytimer = time.time() + RETRYTIME
                    found = []
                    # Try all unknown devices, even if the retry count expired
                    for devid in newdevices:
                        dname = dkey = mac = ""
                        try:
                            (dname, dkey, mac) = tuyaLookup(devid)
                        except:
                            pass

                        if dname != "" or dkey != "":
                            #print('found!', devid, dname, dkey)
                            deviceslist[devid]['name'] = dname
                            deviceslist[devid]['key'] = dkey
                            deviceslist[devid]['mac'] = mac
                            found.append(devid)
                            if devid in retrydevices:
                                del retrydevices[devid]
                    for devid in found:
                        newdevices.remove(devid)

                    # Decrement retry count
                    expired = []
                    for devid in retrydevices:
                        retrydevices[devid] -= 1
                        if retrydevices[devid] < 1:
                            expired.append(devid)
                    for devid in expired:
                        del retrydevices[devid]
            time.sleep(2)
    except (KeyboardInterrupt, SystemExit):
        running = False
        # Close down API thread
        requests.get('http://localhost:%d/stop' % APIPORT)
        print("End")

    # both threads completely executed
    print(json.dumps(deviceslist, indent=4, sort_keys=True))
    print("Done!")
