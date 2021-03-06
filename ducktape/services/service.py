# Copyright 2014 Confluent Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from ducktape.command_line.defaults import ConsoleDefaults
from ducktape.template import TemplateRenderer
from ducktape.errors import TimeoutError
from ducktape.cluster.remoteaccount import RemoteAccount

import os
import shutil
import tempfile
import time


class Service(TemplateRenderer):
    """Service classes know how to deploy a service onto a set of nodes and then clean up after themselves.

    They request the necessary resources from the cluster,
    configure each node, and bring up/tear down the service.

    They also expose
    information about the service so that other services or test scripts can
    easily be configured to work with them. Finally, they may be able to collect
    and check logs/output from the service, which can be helpful in writing tests
    or benchmarks.

    Services should generally be written to support an arbitrary number of nodes,
    even if instances are independent of each other. They should be able to assume
    that there won't be resource conflicts: the cluster tests are being run on
    should be large enough to use one instance per service instance.
    """

    # Provides a mechanism for locating and collecting log files produced by the service on its nodes.
    # logs is a dict with entries that look like log_name: {"path": log_path, "collect_default": boolean}
    #
    # For example, zookeeper service might have self.logs like this:
    # self.logs = {
    #    "zk_log": {"path": "/mnt/zk.log",
    #               "collect_default": True}
    # }
    logs = {}

    def __init__(self, context, num_nodes=None, node_spec=None, *args, **kwargs):
        """
        :param context    An object which has at minimum 'cluster' and 'logger' attributes. In tests, this is always a
                          TestContext object.
        :param num_nodes  An integer representing the number of Linux nodes to allocate. If node_spec is not None, it
                          will be used and num_nodes will be ignored.
        :param node_spec  A dictionary where the key is an operating system (possible values are in
                          ducktape.cluster.remoteaccount.RemoteAccount.SUPPORTED_OS_TYPES) and the value is the number
                          of nodes to allocate for the associated operating system. Values must be integers. Node
                          allocation takes place when start() is called, or when allocate_nodes() is called, whichever
                          happens first.
        """
        super(Service, self).__init__(*args, **kwargs)
        # Keep track of significant events in the lifetime of this service
        self._init_time = time.time()
        self._start_time = -1
        self._start_duration_seconds = -1
        self._stop_time = -1
        self._stop_duration_seconds = -1
        self._clean_time = -1

        self._initialized = False
        self.node_spec = Service.setup_node_spec(num_nodes, node_spec)
        self.context = context

        self.nodes = []
        self.allocate_nodes()

        # Keep track of which nodes nodes were allocated to this service, even after nodes are freed
        # Note: only keep references to representations of the nodes, not the actual node objects themselves
        self._nodes_formerly_allocated = [str(node.account) for node in self.nodes]

        # Every time a service instance is created, it registers itself with its
        # context object. This makes it possible for external mechanisms to clean up
        # after the service if something goes wrong.
        #
        # Note: Allocate nodes *before* registering self with the service registry
        self.context.services.append(self)

        # Each service instance has its own local scratch directory on the test driver
        self._local_scratch_dir = None
        self._initialized = True

    @staticmethod
    def setup_node_spec(num_nodes=None, node_spec=None):
        if not num_nodes and not node_spec:
            raise Exception("Either num_nodes or node_spec must not be None.")

        # If node_spec is none, convert num_nodes to a node_spec dict and assume Linux machines.
        if not node_spec:
            return {RemoteAccount.LINUX: num_nodes}
        else:
            try:
                for os_type, _ in node_spec.iteritems():
                    if os_type not in RemoteAccount.SUPPORTED_OS_TYPES:
                        raise Exception("When nodes is a dictionary, each key must be a " +
                                        "supported OS. '%s' is unknown." % os_type)
                return node_spec
            except:
                raise Exception("Each node_spec key must be a supported operating system: " +
                                "%s, node_spec: %s" % (RemoteAccount.SUPPORTED_OS_TYPES, str(node_spec)))

    def __repr__(self):
        return "<%s: %s>" % (self.who_am_i(), "num_nodes: %d, nodes: %s" %
                             (len(self.nodes), [n.account.hostname for n in self.nodes]))

    @property
    def local_scratch_dir(self):
        """This local scratch directory is created/destroyed on the test driver before/after each test is run."""
        if not self._local_scratch_dir:
            self._local_scratch_dir = tempfile.mkdtemp()
        return self._local_scratch_dir

    @property
    def service_id(self):
        """Human-readable identifier (almost certainly) unique within a test run."""
        return "%s-%d-%d" % (self.__class__.__name__, self._order, id(self))

    @property
    def _order(self):
        """Index of this service instance with respect to other services of the same type registered with self.context.
        When used with a test_context, this lets the user know

        Example:
            suppose the services registered with the same context looks like
                context.services == [Zookeeper, Kafka, Zookeeper, Kafka, MirrorMaker]
            then:
                context.services[0]._order == 0  # "0th" Zookeeper instance
                context.services[2]._order == 0  # "0th" Kafka instance
                context.services[1]._order == 1  # "1st" Zookeeper instance
                context.services[3]._order == 1  # "1st" Kafka instance
                context.services[4]._order == 0  # "0th" MirrorMaker instance
        """
        if hasattr(self.context, "services"):
            same_services = [id(s) for s in self.context.services if type(s) == type(self)]

            if self not in self.context.services and not self._initialized:
                # It's possible that _order will be invoked in the constructor *before* self has been registered with
                # the service registry (aka self.context.services).
                return len(same_services)

            # Note: index raises ValueError if the item is not in the list
            index = same_services.index(id(self))
            return index
        else:
            return 0

    @property
    def logger(self):
        """The logger instance for this service."""
        return self.context.logger

    @property
    def cluster(self):
        """The cluster object from which this service instance gets its nodes."""
        return self.context.cluster

    @property
    def allocated(self):
        """Return True iff nodes have been allocated to this service instance."""
        return len(self.nodes) > 0

    def who_am_i(self, node=None):
        """Human-readable identifier useful for log messages."""
        if node is None:
            return self.service_id
        else:
            return "%s node %d on %s" % (self.service_id, self.idx(node), node.account.hostname)

    def allocate_nodes(self):
        """Request resources from the cluster."""
        if self.allocated:
            raise Exception("Requesting nodes for a service that has already been allocated nodes.")

        self.logger.debug("Requesting nodes from the cluster: %s" % self.node_spec)

        try:
            self.nodes = self.cluster.alloc(self.node_spec)
        except RuntimeError as e:
            msg = str(e.message)
            if hasattr(self.context, "services"):
                msg += " Currently registered services: " + str(self.context.services)
            raise RuntimeError(msg)

        for idx, node in enumerate(self.nodes, 1):
            # Remote accounts utilities should log where this service logs
            if node.account._logger is not None:
                # This log message help test-writer identify which test and/or service didn't clean up after itself
                node.account.logger.critical(ConsoleDefaults.BAD_TEST_MESSAGE)
                raise RuntimeError(
                    "logger was not None on service start. There may be a concurrency issue, " +
                    "or some service which isn't properly cleaning up after itself. " +
                    "Service: %s, node.account: %s" % (self.__class__.__name__, str(node.account)))
            node.account.logger = self.logger

        self.logger.debug("Successfully allocated %d nodes to %s" % (len(self.nodes), self.who_am_i()))

    def start(self):
        """Start the service on all nodes."""
        self.logger.info("%s: starting service" % self.who_am_i())
        if self._start_time < 0:
            # Set self._start_time only the first time self.start is invoked
            self._start_time = time.time()

        self.logger.debug(self.who_am_i() + ": killing processes and attempting to clean up before starting")
        for node in self.nodes:
            # Added precaution - kill running processes, clean persistent files
            # try/except for each step, since each of these steps may fail if there are no processes
            # to kill or no files to remove

            try:
                self.stop_node(node)
            except:
                pass

            try:
                self.clean_node(node)
            except:
                pass

        for node in self.nodes:
            self.logger.debug("%s: starting node" % self.who_am_i(node))
            self.start_node(node)

        if self._start_duration_seconds < 0:
            self._start_duration_seconds = time.time() - self._start_time

    def start_node(self, node):
        """Start service process(es) on the given node."""
        raise NotImplementedError("%s: subclasses must implement start_node." % self.who_am_i())

    def wait(self, timeout_sec=600):
        """Wait for the service to finish.
        This only makes sense for tasks with a fixed amount of work to do. For services that generate
        output, it is only guaranteed to be available after this call returns.
        """
        unfinished_nodes = []
        start = time.time()
        end = start + timeout_sec
        for node in self.nodes:
            now = time.time()
            if end > now:
                self.logger.debug("%s: waiting for node", self.who_am_i(node))
                if not self.wait_node(node, end - now):
                    unfinished_nodes.append(node)
            else:
                unfinished_nodes.append(node)

        if unfinished_nodes:
            raise TimeoutError("Timed out waiting %s seconds for service nodes to finish. " % str(timeout_sec) +
                               "These nodes are still alive: " + str(unfinished_nodes))

    def wait_node(self, node, timeout_sec=None):
        """Wait for the service on the given node to finish. 
        Return True if the node finished shutdown, False otherwise.
        """
        raise NotImplementedError("%s: subclasses must implement wait_node." % self.who_am_i())

    def stop(self):
        """Stop service processes on each node in this service.
        Subclasses must override stop_node.
        """
        self._stop_time = time.time()  # The last time stop is invoked
        self.logger.info("%s: stopping service" % self.who_am_i())
        for node in self.nodes:
            self.logger.info("%s: stopping node" % self.who_am_i(node))
            self.stop_node(node)

        self._stop_duration_seconds = time.time() - self._stop_time

    def stop_node(self, node):
        """Halt service process(es) on this node."""
        raise NotImplementedError("%s: subclasses must implement stop_node." % self.who_am_i())

    def clean(self):
        """Clean up persistent state on each node - e.g. logs, config files etc.
        Subclasses must override clean_node.
        """
        self._clean_time = time.time()
        self.logger.info("%s: cleaning service" % self.who_am_i())
        for node in self.nodes:
            self.logger.info("%s: cleaning node" % self.who_am_i(node))
            self.clean_node(node)

    def clean_node(self, node):
        """Clean up persistent state on this node - e.g. service logs, configuration files etc."""
        self.logger.warn("%s: clean_node has not been overriden. This may be fine if the service leaves no persistent state."
                         % self.who_am_i())

    def free(self):
        """Free each node. This 'deallocates' the nodes so the cluster can assign them to other services."""
        for node in self.nodes:
            self.logger.info("%s: freeing node" % self.who_am_i(node))
            node.account.logger = None
            self.cluster.free(node)

        self.nodes = []

    def run(self):
        """Helper that executes run(), wait(), and stop() in sequence."""
        self.start()
        self.wait()
        self.stop()

    def get_node(self, idx):
        """ids presented externally are indexed from 1, so we provide a helper method to avoid confusion."""
        return self.nodes[idx - 1]

    def idx(self, node):
        """Return id of the given node. Return -1 if node does not belong to this service.

        idx identifies the node within this service instance (not globally).
        """
        for idx, n in enumerate(self.nodes, 1):
            if self.get_node(idx) == node:
                return idx
        return -1

    def close(self):
        """Release resources."""
        # Remove local scratch directory
        if self._local_scratch_dir and os.path.exists(self._local_scratch_dir):
            shutil.rmtree(self._local_scratch_dir)

    @staticmethod
    def run_parallel(*args):
        """Helper to run a set of services in parallel. This is useful if you want
           multiple services of different types to run concurrently, e.g. a
           producer + consumer pair.
        """
        for svc in args:
            svc.start()
        for svc in args:
            svc.wait()
        for svc in args:
            svc.stop()

    def to_json(self):
        return {
            "cls_name": self.__class__.__name__,
            "module_name": self.__module__,

            "lifecycle": {
                "init_time": self._init_time,
                "start_time": self._start_time,
                "start_duration_seconds": self._start_duration_seconds,
                "stop_time": self._stop_time,
                "stop_duration_seconds": self._stop_duration_seconds,
                "clean_time": self._clean_time
            },
            "service_id": self.service_id,
            "nodes": self._nodes_formerly_allocated
        }
