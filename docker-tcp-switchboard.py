#!/usr/bin/env python3

from twisted.protocols.portforward import *
from twisted.internet import reactor

import time, socket, subprocess

# this is a global object that keeps track of the free ports
# when requested, it allocated a new docker instance and returns it

class DockerPorts():
    def __init__(self):
        self.instancesByPort = dict()
        self.instancesByName = dict()
        self.imageParams = dict()

    def registerProxy(self, imagename, outerport, params, limit):
        self.imageParams[outerport] = {
            "imagename": imagename,
            "params": params,
            "limit": limit
        }

    def create(self, outerport):
        imagename = self.imageParams[outerport]["imagename"]
        imagelimit = self.imageParams[outerport]["limit"]
        if imagelimit > 0 and imagename in self.instancesByName:
            icount = len(self.instancesByName[imagename])
            if icount >= imagelimit:
                print("Reached max count of {} (currently {}) for image {}".format(imagelimit, icount, imagename))
                return None

        instance = DockerInstance(imagename, self.imageParams[outerport]["params"])
        instance.start()
        p = instance.middleport
        self.instancesByPort[p] = instance
        if imagename not in self.instancesByName:
            self.instancesByName[imagename] = []
        self.instancesByName[imagename] += [instance]

        return instance

    def destroy(self, instance):
        instance.stop()
        p = instance.middleport
        n = instance.imagename
        del self.instancesByPort[p]
        self.instancesByName[n].remove(instance)

# this class represents a single docker instance listening on a certain middleport.
# The middleport is managed by the DockerPorts global object
# After the docker container is started, we wait until the middleport becomes reachable
# before returning
class DockerInstance():
    def __init__(self, imagename, dockerparams):
        self.dockerparams = dockerparams
        self.imagename = imagename
        self.middleport = None
        self.instanceid = None

    def start(self):
        cmd = "docker run --detach {}".format(self.dockerparams)
        (rc, out) = subprocess.getstatusoutput(cmd.format(0))
        if rc != 0:
            print("Failed to start instance")
            print("rc={}, out={}".format(rc, out))
            return None

        self.instanceid = out.strip()
        cmd = "docker port {}".format(self.instanceid)
        (rc, out) = subprocess.getstatusoutput(cmd)
        if rc != 0:
            print("Failed to get port information from {}".format(self.instanceid))
            print("rc={}, out={}".format(rc, out))
            return None

        try:
            # try to parse something like: "22/tcp -> 0.0.0.0:12345" to extract 12345
            self.middleport = int(out.strip().split(" ")[2].split(":")[1])
        except:
            print("Failed to parse port from returned data for instanceid {}: {}".format(self.instanceid, out))
            self.stop()
            return None

        print("Started instance on middleport {} with ID {}".format(self.middleport, self.instanceid))
        if self.__waitForOpenPort():
            return self.instanceid
        else:
            self.stop()
            return None

    def stop(self):
        print("Killing and removing {} (middleport {})".format(self.instanceid, self.middleport))
        (rc, _) = subprocess.getstatusoutput(("docker kill {}".format(self.instanceid)))
        if rc != 0:
            print("Failed to stop instance for middleport {}, id {}".format(self.middleport, self.instanceid))
            return False
        (rc, _) = subprocess.getstatusoutput(("docker rm {}".format(self.instanceid)))
        if rc != 0:
            print("Failed to remove instance for middleport {}, id {}".format(self.middleport, self.instanceid))
            return False
        return True

    def __isPortOpen(self, readtimeout=0.1):
        s = socket.socket()
        ret = False
        try:
            s.connect(("0.0.0.0", self.middleport))
            # just connecting is not enough, we should try to read and get at least 1 byte back
            # since the daemon in the container might not have started accepting connections yet, while docker-proxy does
            s.settimeout(readtimeout)
            data = s.recv(1)
            ret = len(data) > 0
        except socket.error:
            ret = False

        s.close()
        return ret

    def __waitForOpenPort(self, timeout=5, step=0.1):
        started = time.time()

        while started + timeout >= time.time():
            if self.__isPortOpen():
                return True
            time.sleep(step)
        return False
        

class DockerProxyServer(ProxyServer):
    clientProtocolFactory = ProxyClientFactory
    reactor = None

    # This is a reimplementation, except that we want to specify host and port...
    def connectionMade(self): 
        # Don't read anything from the connecting client until we have
        # somewhere to send it to.
        self.transport.pauseProducing()

        client = self.clientProtocolFactory()
        client.setServer(self)

        if self.reactor is None:
            from twisted.internet import reactor
            self.reactor = reactor
        global globalDockerPorts
        self.dockerinstance = globalDockerPorts.create(self.factory.outerport)
        if self.dockerinstance == None:
            self.transport.write(bytearray("Maximum connection-count reached. Try again later.\r\n", "utf-8"))
            self.transport.loseConnection()
        else:
            self.reactor.connectTCP("0.0.0.0", self.dockerinstance.middleport, client)

    def connectionLost(self, reason):
        if self.dockerinstance != None:
            global globalDockerPorts
            globalDockerPorts.destroy(self.dockerinstance)
        self.dockerinstance = None
        super().connectionLost(reason)

class DockerProxyFactory(ProxyFactory):
    protocol = DockerProxyServer

    def __init__(self, outerport):
        self.outerport = outerport

def readConfig(fn):
    import configparser, glob
    # read the configfile.
    config = configparser.ConfigParser()
    print("Reading configfile from {}".format(fn))
    config.read(fn)

    # if there is a configdir directory, reread everything
    if "global" in config.sections() and "splitconfigfiles" in config["global"]:
        fnlist = [fn] + [f for f in glob.glob(config["global"]["splitconfigfiles"])]
        print("Detected configdir directive. Reading configfiles from {}".format(fnlist))
        config = configparser.ConfigParser()
        config.read(fnlist)

    return config


if __name__ == "__main__":
    import sys

    config = readConfig(sys.argv[1] if len(sys.argv) > 1 else '/etc/docker-tcp-switchboard.conf')

    if len(config.sections()) == 0 or (len(config.sections()) == 1 and "global" in config.sections()):
        print("invalid configfile. No docker images")
        sys.exit(1)

    globalDockerPorts = DockerPorts()

    for imagesection in [n for n in config.sections() if n != "global"]:
        outerport = int(config[imagesection]["outerport"])
        globalDockerPorts.registerProxy(imagesection, outerport,
            config[imagesection]["dockerparams"],
            int(config[imagesection]["limit"])
            )

        print("Listening on port {}".format(outerport))
        reactor.listenTCP(outerport, DockerProxyFactory(outerport))
    reactor.run()


