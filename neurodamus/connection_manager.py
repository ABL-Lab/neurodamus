from __future__ import absolute_import, print_function
import logging
from itertools import chain
from os import path
from .core import NeuronDamus as ND, MPI
from .core import ProgressBarRank0 as ProgressBar
from .utils import compat, bin_search, OrderedDefaultDict
from .utils.logging import log_verbose
from .connection import SynapseParameters, Connection, SynapseMode, STDPMode


class _ConnectionManagerBase(object):
    """
    An abstract base class common to Synaptic connections and GapJunctions
    """
    DATA_FILENAME = "nrn.h5"
    # Synapses dont require circuit_target but GapJunctions do
    # so the generic insertion validates against target.sgid if defined
    _circuit_target = None
    # Connection parameters which might not be used by all Managers
    _synapse_mode = SynapseMode.default

    def __init__(self, circuit_path, target_manager, n_synapse_files):
        self._target_manager = target_manager
        # Connections indexed by post-gid, then ordered by pre-gid
        self._connections_map = OrderedDefaultDict()
        self._disabled_conns = OrderedDefaultDict()
        # Cur loaded file
        self._syn_params = {}  # Parameters cache by post-gid (previously loadedMap)
        self._synapse_reader = None
        self._nrn_version = None

        synapse_file = path.join(circuit_path, self.DATA_FILENAME)
        self.open_synapse_file(synapse_file, n_synapse_files)

    def open_synapse_file(self, synapse_file, n_synapse_files):
        log_verbose("Loading synapses...")
        self._synapse_reader = reader = self._init_synapse_reader(
            self._target_manager, synapse_file, n_synapse_files)
        self._n_synapse_files = n_synapse_files
        self._syn_params = {}  # purge cache
        self._nrn_version = reader.checkVersion()

    @staticmethod
    def _init_synapse_reader(target_manager, synapse_file, n_synapse_files):
        _syn_reader = ND.HDF5Reader(synapse_file, n_synapse_files)

        if n_synapse_files > 1:
            gidvec = ND.Vector()
            for gid in target_manager.cellDistributor.getGidListForProcessor():
                gidvec.append(gid)
            _syn_reader.exchangeSynapseLocations(gidvec)
        return _syn_reader

    # -
    def connect_all(self, gidvec, weight_factor=1):
        """For every gid access its synapse parameters and instantiate all synapses.

        Args:
            gidvec: The array of local gids
            weight_factor: (Optional) factor to scale all netcon weights
        """
        log_verbose("Creating connections from synapse params file (NRN)...")
        total_created_conns = 0

        for tgid in ProgressBar.iter(gidvec):
            synapses_params = self.get_synapse_parameters(tgid)
            cur_conn = None
            logging.debug("Connecting post neuron a%d: %d synapses", tgid, len(synapses_params))
            gid_created_conns = 0

            for i, syn_params in enumerate(synapses_params):
                sgid = int(syn_params.sgid)
                if self._circuit_target and not self._circuit_target.completeContains(sgid):
                    continue

                # should still need to check that the other side of the gap junction will
                # be there by ensuring that other gid is in the circuit target
                # Note: The sgids in any given dataset from nrn.h5 will come in sorted order,
                # low to high. This code therefore doesn't search or sort on its own.
                # If the nrn.h5 file changes in the future we must update the code accordingly

                if cur_conn is None or cur_conn.sgid != sgid:
                    cur_conn = Connection(
                        sgid, tgid, weight_factor, None, STDPMode.NO_STDP, 0, self._synapse_mode)
                    # Store immediately, even though we still append synapses
                    self.store_connection(cur_conn)
                    gid_created_conns += 1

                # placeSynapses() called from connection.finalize
                point = self._target_manager.locationToPoint(
                    tgid, syn_params.isec, syn_params.ipt, syn_params.offset)
                cur_conn.add_synapse(point, syn_params, i)

                # if _debug_conn == (tgid, sgid):
                #     logging.debug("  -> Params (sgid, D, F, location): %d, %d, %d, %f",
                #                   syn_params.sgid, syn_params.D, syn_params.F, syn_params.location

            if gid_created_conns > 0:
                logging.debug("[post-gid %d] Created %d connections", tgid, gid_created_conns)
                total_created_conns += gid_created_conns

        log_verbose("ConnectAll: Created %d connections", total_created_conns)

    # Compatibility
    connectAll = connect_all

    # -
    def group_connect(self, src_target, dst_target, gidvec, weight_factor=None, configuration=None,
                      stdp_mode=None, spont_mini_rate=0, synapse_types=None, synapse_override=None,
                      creation_mode=True):
        """Given source and destination targets, create all connections for post-gids in gidvec.
        Note: the cells in the source list are not limited by what is on this cpu whereas
        the dest list requires the cells be local

        Args:
            src_target: Name of Source Target
            dst_target: Name of Destination Target
            gidvec: Vector of gids on the local cpu
            weight_factor: (float) Scaling weight to apply to the synapses. Default: dont change
            configuration: (str) SynapseConfiguration Default: None
            stdp_mode: Which STDP to use. Default: None (=TDPoff for creating, wont change existing)
            spont_mini_rate: (float) For spontaneous minis trigger rate (default: 0)
            synapse_types: (tuple) To restrict which synapse types are created. Default: None
            synapse_override: An alternative point process configuration.
            creation_mode: By default new connections are created. If False updates existing only
        """
        # unlike connectAll, we must look through self._connections_map to see if sgid->tgid exists
        # because it may be getting weights updated.
        # Note: it is better to get the whole target over just the gid vector since then we can use
        # utility functions like 'contains'
        src_target = self._target_manager.getTarget(src_target)
        dst_target = self._target_manager.getTarget(dst_target)
        stdp = STDPMode.from_str(stdp_mode) if stdp_mode is not None else None
        synapses_restrict = synapse_types is not None
        total_created_conns = 0

        if synapses_restrict and not isinstance(synapse_types, (tuple, list)):
            synapse_types = (synapse_types,)

        for tgid in gidvec:
            if not dst_target.contains(tgid):
                continue

            # this cpu owns some or all of the destination gid
            syns_params = self.get_synapse_parameters(tgid)
            gid_created_conns = 0
            prev_sgid = None
            pend_conn = None

            for i, syn_params in enumerate(syns_params):
                if synapses_restrict and syn_params.synType not in synapse_types:
                    continue

                # if this gid in the source target?
                sgid = int(syn_params.sgid)
                if not src_target.completeContains(sgid):
                    continue

                # is this gid in the self._circuit_target (if defined)
                if self._circuit_target and not self._circuit_target.completeContains(sgid):
                    continue

                # When the sgdig changes we are in a new connection. Can save the previous
                if sgid != prev_sgid:
                    if pend_conn:
                        self.store_connection(pend_conn)
                        pend_conn = None
                    prev_sgid = sgid

                    # determine what we will do with the new sgid
                    # update params if seen before, or create connection
                    cur_conn = self.get_connection(sgid, tgid)  # type: Connection

                    if cur_conn is not None:
                        if weight_factor is not None:
                            cur_conn.weight_factor = weight_factor
                        if configuration is not None:
                            cur_conn.add_synapse_configuration(configuration)
                        if stdp is not None:
                            cur_conn.stdp = stdp
                        if synapse_override is not None:
                            cur_conn.override_synapse(synapse_override)
                    else:
                        if creation_mode:
                            if weight_factor is None:
                                logging.warning("Conn %d->%d. Invalid weight_factor. Assuming 1.0",
                                                sgid, tgid)
                            pend_conn = Connection(
                                sgid, tgid, weight_factor, configuration, stdp, spont_mini_rate,
                                self._synapse_mode, synapse_override)
                            gid_created_conns += 1

                # if we have a pending connection we place the current synapse(s)
                if pend_conn is not None:
                    point = self._target_manager.locationToPoint(
                        tgid, syn_params.isec, syn_params.ipt, syn_params.offset)
                    pend_conn.add_synapse(point, syn_params, i)

            # store any remaining pending connection
            if pend_conn is not None:
                self.store_connection(pend_conn)

            # Info ------
            if gid_created_conns > 0:
                logging.debug("[post-gid %d] Created %d connections", tgid, gid_created_conns)
                total_created_conns += gid_created_conns

        if total_created_conns > 0:
            log_verbose("Group: Created %d connections", total_created_conns)

    # -
    def get_synapse_parameters(self, gid):
        """Access the specified dataset from the nrn.h5 file to get all synapse parameters for
        a post-synaptic cell

        Args:
            gid: The gid of the cell whose parameters are required

        Returns: A list containing the parameters (SynapseParameters objects) of each synapse
        """
        if gid in self._syn_params:
            return self._syn_params[gid]  # Cached

        reader = self._synapse_reader
        cell_name = "a%d" % gid

        if self._n_synapse_files > 1:
            ret = reader.loadData(gid)
        else:
            ret = reader.loadData(cell_name)

        if ret < 0:
            logging.debug("No synapses for post-gid %d. Skipping", gid)
            self._syn_params[gid] = []
            return []

        nrow = int(reader.numberofrows(cell_name))
        conn_syn_params = SynapseParameters.create_array(nrow)
        self._syn_params[gid] = conn_syn_params
        dt = ND.dt

        for i in range(nrow):
            params = conn_syn_params[i]
            params[0] = reader.getData(cell_name, i, 0)   # sgid
            params[1] = reader.getData(cell_name, i, 1)   # delay
            params[2] = reader.getData(cell_name, i, 2)   # isec
            params[3] = reader.getData(cell_name, i, 3)   # ipt
            params[4] = reader.getData(cell_name, i, 4)   # offset
            params[5] = reader.getData(cell_name, i, 8)   # weight
            params[6] = reader.getData(cell_name, i, 9)   # U
            params[7] = reader.getData(cell_name, i, 10)  # D
            params[8] = reader.getData(cell_name, i, 11)  # F
            params[9] = reader.getData(cell_name, i, 12)  # DTC
            params[10] = reader.getData(cell_name, i, 13)  # isynType
            if self._nrn_version > 4:
                params[11] = reader.getData(cell_name, i, 17)  # nrrp
            else:
                params[11] = -1
            # compensate for minor floating point inaccuracies in the delay
            params[1] = int(params[1] / dt + 1e-5) * dt

        return conn_syn_params

    # -
    def configure_connection(self, src_target, dst_target, gidvec,
                             configuration=None, weight=None, **syn_params):
        """ Given some gidlists, recover the connection objects for those gids involved and
        adjust params.
        NOTE: Keyword arguments override the same-name properties in the provided hoc configuration

        Args:
            src_target: Name of Source Target
            dst_target: Name of Destination Target
            gidvec: A list of gids to apply configuration
            configuration: (optional) A hoc configuration str to be executed over synapse objects
            weight: (optional) new weights for the netcons
            **syn_params: Keyword arguments of synapse properties to be changed, e.g. conductance(g)
        """
        if configuration is None and weight is None and not syn_params:
            logging.warning("No parameters adjustement being made to synpases in Targets %s->%s",
                            src_target, dst_target)
            return

        # unlike connectAll, we must look through self._connections_map to see if sgid->tgid exists
        # because it may be getting weights updated. Note that it is better to get the whole target
        # over just the gid vector, since then we can use utility functions like 'contains'
        src_target = self._target_manager.getTarget(src_target)
        dst_target = self._target_manager.getTarget(dst_target)

        for tgid in gidvec:
            if not dst_target.contains(tgid):
                continue

            sgids = src_target.completegids()
            for sgid in sgids:
                sgid = int(sgid)
                conn = self.get_connection(sgid, tgid)
                if conn is not None:
                    # change params for all synapses
                    if configuration is not None:
                        conn.configure_synapses(configuration)
                    if syn_params:
                        conn.update_synapse_params(**syn_params)
                    # Change params for all netcons
                    if weight is not None:
                        conn.update_weights(weight)

    # -
    def configure_connection_config(self, conn_parsed_config, gidvec):
        """Change connection configuration after finalize, according to parsed config

        Args:
            conn_parsed_config: The parsed connection configuration object
            gidvec: A list of gids to apply configuration
        """
        src_target = conn_parsed_config.get("Source").s
        dst_target = conn_parsed_config.get("Destination").s

        weight = conn_parsed_config.valueOf("Weight") \
            if conn_parsed_config.exists("Weight") else None
        config = conn_parsed_config.get("SynapseConfigure").s \
            if conn_parsed_config.exists("SynapseConfigure") else None

        self.configure_connection(src_target, dst_target, gidvec, config, weight)

    # Global update Helpers
    # ---------------------
    def update_weights(self, new_weight, also_replay_netcons=False):
        for conn in self.all_connections():  # type: Connection
            conn.update_weights(new_weight, also_replay_netcons)

    def update_parameters_all(self, **params):
        for conn in self.all_connections():  # type: Connection
            conn.update_synapse_params(**params)

    def update_conductances(self, new_g):
        self.update_parameters_all(g=new_g)

    # -----------------------------------------------------------------------------
    # THESE METHODS ARE HELPERS FOR HANDLING OBJECTS IN THE INTERNAL STRUCTURES
    # -----------------------------------------------------------------------------
    def _find_connection(self, sgid, tgid, exact=True):
        """Finds a connection, given its source and destination gids.
        Returns:
            tuple: connection list and index. If the element doesnt exist, index depends on the
            exact flag: None if exact=True, otherwise the possible insertion index.
        """
        cell_conns = self._connections_map[tgid]
        pos = 0
        if cell_conns:
            pos = bin_search(cell_conns, sgid, lambda x: x.sgid)
        if exact and (pos == len(cell_conns) or cell_conns[pos].sgid != sgid):
            # Not found
            return cell_conns, None
        return cell_conns, pos

    # -
    def get_connection(self, sgid, tgid):
        """Retrieves a connection from the pre and post gids.
        Returns:
            Connection: A connection object if it exists. None otherwise.
        """
        conn_lst, idx = self._find_connection(sgid, tgid)
        return None if idx is None else conn_lst[idx]

    # -
    def store_connection(self, conn):
        """When we have created a new connection (sgid->tgid), store it in order in
        our structure for faster retrieval later

        Args:
            conn: The connection object to be stored
        """
        cell_conns, pos = self._find_connection(conn.sgid, conn.tgid, exact=False)
        if pos < len(cell_conns) and cell_conns[pos].sgid == conn.sgid:
            logging.error("Attempt to store existing connection: %d->%d", conn.sgid, conn.tgid)
            return
        cell_conns.insert(pos, conn)

    # -
    def all_connections(self):
        """Get an iterator over all the connections.
        """
        return chain.from_iterable(self._connections_map.values())

    def get_connections(self, target_gid):
        """Get an iterator over all the connections of a target cell
        """
        return self._connections_map[target_gid]

    def get_synapse_params_gid(self, target_gid):
        """Get an iterator over all the synapse parameters of a target cell
        """
        conns = self._connections_map[target_gid]
        return chain.from_iterable(c.synapse_params for c in conns)

    # -----------------------------------------------------------------------------
    # Delete, Disable / Enable
    # -----------------------------------------------------------------------------
    def delete(self, sgid, tgid):
        """Deletes a connection given source and target gids.
        This action can't be undone. To have the connection again it must be recreated.
        For temporarily ignoring a connection see disable()
        NOTE: Contrary to disable(), deleting a connection will effectively remove them
        from the model, saving computational resources during simulation.
        """
        conn_lst, idx = self._find_connection(sgid, tgid)
        if idx is None:
            logging.warning("Non-existing connection to disable: %d->%d", sgid, tgid)
            return
        del conn_lst[idx]

    # -
    def disable(self, sgid, tgid, also_zero_conductance=False):
        """Disable a connection, all of its netcons and optionally synapses.

        Args:
            sgid: The pre-gid of the cell
            tgid: The post-gid of the cell
            also_zero_conductance: (bool) Besides deactivating the netcon, will set synapses'
                conductances to zero. Default: False
        """
        conn_lst, idx = self._find_connection(sgid, tgid)
        if idx is None:
            logging.warning("Non-existing connection to disable: %d->%d", sgid, tgid)
        else:
            c = conn_lst.pop(idx)  # type: Connection
            self._disabled_conns[tgid].append(c)
            c.disable(also_zero_conductance)

    # -
    def enable(self, sgid, tgid):
        """(Re)enable a connection
        """
        tgid_conns = self._disabled_conns[tgid]
        for i, c in enumerate(tgid_conns):  # type: Connection
            if c.sgid == sgid:
                self.store_connection(c)
                del tgid_conns[i]
                c.enable()
                break
        else:
            logging.warning("Non-existing connection to enable: %d->%d", sgid, tgid)

    # -
    def delete_group(self, post_gids, pre_gids=None):
        """Delete a number of connections given lists of pre and post gids.
           Note: None is neutral and will match all gids.

        Args:
            post_gids: The target gids of the connections to be disabled. None for all
            pre_gids: idem for pre-gids. [Default: None -> all)
        """
        for _, lst, idx in self._find_group_in(self._connections_map, post_gids, pre_gids):
            del lst[idx]

    # -
    def disable_group(self, post_gids, pre_gids=None, also_zero_conductance=False):
        """Disable a number of connections given lists of pre and post gids.
        Note: None will match all gids.

        Args:
            post_gids: The target gids of the connections to be disabled. None for all
            pre_gids: idem for pre-gids. [Default: None -> all)
            also_zero_conductance: Besides disabling the netcon, sets synapse conductance to 0
        """
        for conn, lst, idx in self._find_group_in(self._connections_map, post_gids, pre_gids):
            self._disabled_conns[conn.tgid].append(lst.pop(idx))
            conn.disable(also_zero_conductance)

    # -
    def enable_group(self, post_gids, pre_gids=None):
        """Enable a number of connections given lists of pre and post gids.
        Note: None will match all gids.
        """
        for conn, lst, idx in self._find_group_in(self._disabled_conns, post_gids, pre_gids):
            self.store_connection(lst.pop(idx))
            conn.enable()

    def all_disabled(self):
        return chain.from_iterable(self._disabled_conns.values())

    def get_disabled(self, post_gid):
        return self._disabled_conns[post_gid]

    # -
    @staticmethod
    def _find_group_in(conn_map, post_gids, pre_gids=None):
        # type: (dict, list, list) -> []
        for tgid, conns in conn_map.items():
            if post_gids is not None and tgid not in post_gids:
                continue
            for i, c in enumerate(conns):  # type: Connection
                if pre_gids is not None and c.sgid not in pre_gids:
                    continue
                yield c, conns, i


# ################################################################################################
# SynapseRuleManager
# ################################################################################################
class SynapseRuleManager(_ConnectionManagerBase):
    """
    The SynapseRuleManager is designed to encapsulate the creation of synapses for BlueBrain
    simulations, handling the data coming from the nrn.h5 file. If the BlueConfig file provides any
    Connection Rules, those override which synapses are created.
    Note that the Connection rules are processed with the assumption that they come in the config
    file from more general to more specific. E.g.: A column->column connection should come before
    layer 4 -> layer 2 which should come before L4PC -> L2PC.
    Once all synapses are preped with final weights, the netcons can be created.
    """

    def __init__(self, circuit_path, target_manager, n_synapse_files, synapse_mode=None):
        """ Constructor for SynapseRuleManager, checks that the nrn.h5 synapse file is available
        for reading

        Args:
            circuit_path: Circuit path ('ncsStructural_gp2/nrn.h5' is added by this function)
            target_manager: The TargetManager which will be used to query targets and translate
                locations to points
            n_synapse_files: How many nrn.h5 files to expect (typically 1)
            synapse_mode: str dictating modifiers to what synapses are placed based on synType
                (AmpaOnly vs DualSyns). Default: DualSyns
        """
        _ConnectionManagerBase.__init__(self, circuit_path, target_manager, n_synapse_files)
        self._synapse_mode = SynapseMode.from_str(synapse_mode) \
            if synapse_mode is not None else SynapseMode.default

        #  self._rng_list = []
        self._replay_list = []

    # -
    def finalize(self, base_seed=0):
        """Create the actual synapses and netcons.
        All weight scalars should have their final values.

        Args:
            base_seed: optional argument to adjust synapse RNGs (default=0)
        """
        cell_distributor = self._target_manager.cellDistributor

        log_verbose("Instantiating synapses...")
        for tgid, conns in ProgressBar.iteritems(self._connections_map):
            metype = cell_distributor.getMEType(tgid)
            spgid = cell_distributor.getSpGid(tgid)
            for conn in conns:  # type: Connection
                conn.finalize(cell_distributor.pnm, metype, base_seed, spgid)
            logging.debug("Created %d connections on post-gid %d", len(conns), tgid)

    # compat
    finalizeSynapses = finalize

    # -
    def replay(self, target_name, spike_map):
        """ After all synapses have been placed, we can create special netcons to trigger
        events on those synapses

        Args:
            target_name: Target name whose gids should be replayed
            spike_map: map of gids (pre-synaptic) with vector of spike times
        """
        log_verbose("Applying replay map with %d src cells...", len(spike_map))
        target = self._target_manager.getTarget(target_name)
        used_src_gids = set()

        for tgid, conns in ProgressBar.iteritems(self._connections_map):
            if not target.contains(tgid):
                continue

            for conn in conns:
                if conn.sgid in spike_map:
                    conn.replay(spike_map[conn.sgid])
                    self._replay_list.append(conn)
                    used_src_gids.add(conn.sgid)

        n_added_spike_src = len(used_src_gids)
        if n_added_spike_src == 0:
            logging.warning("[CPU #%2d] No cells were injected replay stimulus", MPI.rank)
        else:
            log_verbose("Cpu0: Added replays to %d src cells" % n_added_spike_src)


# ################################################################################################
# Gap Junctions
# ################################################################################################
class GapJunctionManager(_ConnectionManagerBase):
    """
    The GapJunctionManager is similar to the SynapseRuleManager. It will open special nrn.h5 files
    which will have the locations and conductance strengths of gap junctions detected in the
    circuit. The user will have the capacity to scale the conductance weights
    """
    DATA_FILENAME = "nrn_gj.h5"  # override

    def __init__(self, circuit_path, target_manager, n_synapse_files, circuit_target=None):
        """Constructor for GapJunctionManager, checks that the nrn_gj.h5 synapse file is available
        for reading

        Args:
            circuit_path: Circuit path ('ncsStructural_gp2/nrn.h5' is added by this function)
            target_manager: The TargetManager which will be used to query targets and
                translate locations to points
            n_synapse_files: How many nrn.h5 files to expect (typically 1)
            circuit_target: (optional) Used to know if a given gid is being simulated,
                including off node. Default: full circuit
        """
        _ConnectionManagerBase.__init__(self, circuit_path, target_manager, n_synapse_files)

        self._circuit_target = circuit_target
        self._gj_offsets = compat.Vector("d")
        gjfname = path.join(circuit_path, "gjinfo.txt")
        gj_sum = 0

        for line in open(gjfname):
            gid, offset = line.strip().split()
            self._gj_offsets.append(gj_sum)
            gj_sum += 2 * offset

    # -
    def finalize(self):
        """Creates the netcons for all GapJunctions.
        Connections must have been places and all weight scalars should have their final values.
        """
        for conn in self.all_connections():  # type: Connection
            metype = self._target_manager.cellDistributor.getMEType(conn.tgid)
            conn.finalize_gap_junctions(
                ND.pnm, metype, self._gj_offsets[conn.tgid-1], self._gj_offsets[conn.sgid-1])

    # Compat
    finalizeGapJunctions = finalize
