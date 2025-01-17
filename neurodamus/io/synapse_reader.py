"""
Module implementing interfaces to the several synapse readers (eg.: synapsetool, Hdf5Reader)
"""
import logging
import os
from abc import abstractmethod

import libsonata
import numpy as np

from ..core import NeurodamusCore as Nd, MPI
from ..utils.logging import log_verbose


def _get_sonata_circuit(path):
    """Returns a SONATA edge file in path if present
    """
    if os.path.isdir(path):
        filename = os.path.join(path, "edges.sonata")
        if os.path.exists(filename):
            return filename
    elif path.endswith(".sonata"):
        return path
    elif path.endswith(".h5"):
        import h5py
        f = h5py.File(path, 'r')
        if "edges" in f:
            return path
    return None


def _constrained_hill(K_half, y):
    K_half_fourth = K_half**4
    y_fourth = y**4
    return (K_half_fourth + 16) / 16 * y_fourth / (K_half_fourth + y_fourth)


class _SynParametersMeta(type):
    def __init__(cls, name, bases, attrs):
        type.__init__(cls, name, bases, attrs)
        # Init public properties of the class
        assert hasattr(cls, "_synapse_fields"), "Please define _synapse_fields class attr"
        cls.dtype = np.dtype({"names": cls._synapse_fields,
                              "formats": ["f8"] * len(cls._synapse_fields)})
        cls.empty = np.recarray(0, cls.dtype)


class SynapseParameters(metaclass=_SynParametersMeta):
    """Synapse parameters, internally implemented as numpy record
    """
    _synapse_fields = ("sgid", "delay", "isec", "ipt", "offset", "weight", "U", "D", "F",
                       "DTC", "synType", "nrrp", "u_hill_coefficient", "conductance_ratio",
                       "maskValue", "location")  # total: 16

    def __new__(cls, *_):
        raise NotImplementedError()

    @classmethod
    def create_array(cls, length):
        npa = np.recarray(length, cls.dtype)
        npa.conductance_ratio = -1  # set to -1 (not-set). 0 is meaningful
        npa.maskValue = -1
        npa.location = 0.5
        return npa

    @classmethod
    def concatenate(cls, syn_params, extra_syn_params):
        from numpy.lib.recfunctions import merge_arrays
        new_params = merge_arrays((syn_params, extra_syn_params), asrecarray=True, flatten=True)
        return new_params


class SynapseReader(object):
    """ Synapse Readers base class.
        Factory create() will attempt to instantiate SynReaderSynTool, followed by SynReaderNRN.
    """
    # Data types to read
    SYNAPSES = 0
    GAP_JUNCTIONS = 1

    def __init__(self, src, conn_type, population=None, *_, **kw):
        self._conn_type = conn_type
        self._ca_concentration = kw.get("extracellular_calcium")
        self._syn_params = {}  # Parameters cache by post-gid (previously loadedMap)
        self._open_file(src, population, kw.get("verbose", False))
        # NOTE u_hill_coefficient and conductance_scale_factor are optional, BUT
        # while u_hill_coefficient can always be readif avail, conductance reader may not.
        self._uhill_property_avail = self.has_property("u_hill_coefficient")
        self._extra_fields = tuple()
        self._extra_fields_parameters = None
        self._extra_scale_vars = []

    def preload_data(self, ids):
        pass

    def configure_override(self, mod_override):
        if not mod_override:
            return

        override_helper = mod_override + "Helper"
        Nd.load_hoc(override_helper)

        # Read attribute names with format "attr1;attr2;attr3"
        attr_names = getattr(Nd, override_helper + "_NeededAttributes", None)
        if attr_names:
            log_verbose('Reading parameters "{}" for mod override: {}'.format(
                ", ".join(attr_names.split(";")), mod_override))

            class CustomSynapseParameters(SynapseParameters):
                _synapse_fields = tuple(attr_names.split(";"))

            self._extra_fields = tuple(attr_names.split(";"))
            self._extra_fields_parameters = CustomSynapseParameters

        # Read attribute names with format "attr1;attr2;attr3"
        attr_names = getattr(Nd, override_helper + "_UHillScaleVariables", None)
        if attr_names:
            self._extra_scale_vars = attr_names.split(";")

    def get_synapse_parameters(self, gid):
        """Obtains the synapse parameters record for a given gid.
        """
        syn_params = self._syn_params.get(gid)
        if syn_params is None:
            syn_params = self._load_synapse_parameters(gid)

            # Modify parameters
            self._patch_delay_fp_inaccuracies(syn_params)
            if self._uhill_property_avail:
                self._scale_U_param(syn_params, self._ca_concentration, self._extra_scale_vars)
            self._syn_params[gid] = syn_params  # cache parameters
        return syn_params

    @abstractmethod
    def _load_synapse_parameters(self, gid):
        """The low level reading of synapses subclasses must override"""
        pass

    @staticmethod
    def _patch_delay_fp_inaccuracies(records):
        if len(records) == 0 or 'delay' not in records.dtype.names:
            return
        dt = Nd.dt
        records.delay = (records.delay / dt + 1e-5).astype('i4') * dt

    @staticmethod
    def _scale_U_param(syn_params, extra_cellular_calcium, extra_scale_vars):
        if len(syn_params) == 0:
            return
        if extra_cellular_calcium is None:
            return

        scale_factors = _constrained_hill(syn_params.u_hill_coefficient,
                                          extra_cellular_calcium)
        syn_params.U *= scale_factors

        for scale_var in extra_scale_vars:
            syn_params[scale_var] *= scale_factors

    @abstractmethod
    def _open_file(self, src, population, verbose=False):
        """Initializes the reader, opens the synapse file
        """

    @abstractmethod
    def has_nrrp(self):
        """Checks whether source data has the nrrp field.
        """

    @abstractmethod
    def has_property(self, field_name):
        """Checks whether source data has the given additional field.
        """

    @classmethod
    def create(cls, syn_src, conn_type=SYNAPSES, population=None, *args, **kw):
        """Instantiates a synapse reader, giving preference to SynReaderSynTool
        """
        # If create called from this class then FACTORY, try SynReaderSynTool
        if cls is SynapseReader:
            kw["verbose"] = (MPI.rank == 0)
            if fn := _get_sonata_circuit(syn_src):
                log_verbose("[SynReader] Using SonataReader.")
                return SonataReader(fn, conn_type, population, **kw)
            else:
                if not os.path.isdir(syn_src) and not syn_src.endswith(".h5"):
                    raise SynToolNotAvail(
                        "Can't load new synapse formats without syntool. File: {}".format(syn_src))
                logging.info("[SynReader] Attempting legacy hdf5 reader.")
                return SynReaderNRN(syn_src, conn_type, None, *args, **kw)
        else:
            return cls(syn_src, conn_type, population, *args, **kw)


class SonataReader(SynapseReader):
    """Reader for SONATA edge files.

    Uses libsonata directly and contains a bunch of workarounds to accomodate files
    created in the transition to SONATA.  Also translates all GIDs from 0-based as on disk
    to the 1-based convention in Neurodamus.

    Will read each attribute for multiple GIDs at once and cache read data in a columnar
    fashion.

    FIXME Remove the caching at the np.recarray level.
    """
    SYNAPSE_INDEX_NAMES = set(["synapse_index"])

    def _open_file(self, src, population, _):
        storage = libsonata.EdgeStorage(src)
        if not population:
            assert len(storage.population_names) == 1
            population = next(iter(storage.population_names))
        self._population = storage.open_population(population)
        self._data = {}

    def has_nrrp(self):
        """This field is required in SONATA."""
        return True

    def has_property(self, field_name):
        if field_name in self.SYNAPSE_INDEX_NAMES:
            return True
        return field_name in self._population.attribute_names

    def get_property(self, gid, field_name):
        """Retrieves a full pre-loaded property given a gid and the property name.
        """
        return self._data[gid][field_name]

    def preload_data(self, ids):
        """Preload SONATA fields for the specified IDs"""
        needed_ids = sorted(set(ids) - set(self._data.keys()))
        needed_edge_ids = self._population.afferent_edges([gid - 1 for gid in needed_ids])

        tgids = self._population.target_nodes(needed_edge_ids) + 1
        sgids = self._population.source_nodes(needed_edge_ids) + 1

        def _populate(field, data):
            for gid in needed_ids:
                idx = tgids == gid
                self._data.setdefault(gid, {})[field] = data[idx]

        def _read(attribute, optional):
            if attribute in self._population.attribute_names:
                return self._population.get_attribute(attribute, needed_edge_ids)
            elif optional:
                if attribute:
                    log_verbose("Defaulting to -1.0 for attribute %s", attribute)
                # Without the dtype, will default to unsigned int like tgids and
                # underflow!
                return np.full_like(tgids, -1.0, dtype="f8")
            else:
                raise AttributeError(f"Missing attribute {attribute} in the SONATA edge file")

        _populate("tgid", tgids)
        _populate("sgid", sgids)

        # Synaptic properties
        _populate("delay", _read("delay", False))
        _populate("weight", _read("conductance", False))
        _populate("U", _read("u_syn", False))
        _populate("D", _read("depression_time", False))
        _populate("F", _read("facilitation_time", False))
        _populate("DTC", _read("decay_time", False))
        _populate("synType", _read("syn_type_id", False))
        _populate("nrrp", _read("n_rrp_vesicles", False))

        # These two attributes were added later and are considered optional
        _populate("u_hill_coefficient", _read("u_hill_coefficient", True))
        _populate("conductance_ratio", _read("conductance_scale_factor", True))

        # Make synapse index in the file explicit
        for name in self.SYNAPSE_INDEX_NAMES:
            _populate(name, needed_edge_ids.flatten())

        # Position of the synapse
        if self.has_property("afferent_section_id"):
            _populate("isec", _read("afferent_section_id", False))
            # SONATA compliant synapse position: (section, section_fraction) takes precedence
            # over the older (section, segment, segment_offset) synapse position.
            #
            # Re-using field names for historical reason.
            # FIXME Use dedicated fields
            if self.has_property("afferent_section_pos"):
                # None shan't ever be in the `attribute_names` → defaults to -1.0
                _populate("ipt", _read(None, True))
                _populate("offset", _read("afferent_section_pos", False))
            # This was a temporary naming scheme
            # FIXME Circuits using this field should be fixed
            elif self.has_property("afferent_section_fraction"):
                logging.warning(
                    "Circuit uses non-standard compliant attribute `afferent_section_fraction`"
                )
                # None shan't ever be in the `attribute_names` → defaults to -1.0
                _populate("ipt", _read(None, True))
                _populate("offset", _read("afferent_section_fraction", False))
            else:
                logging.warning(
                    "Circuit is missing standard compliant attribute `afferent_section_pos`"
                )
                _populate("ipt", _read("afferent_segment_id", False))
                _populate("offset", _read("afferent_segment_offset", False))
        else:
            # FIXME All this should go the way of the dodo
            logging.warning(
                "Circuit uses attribute notation using `morpho_` and is not SONATA compliant"
            )
            _populate("isec", _read("morpho_section_id_post", False))
            if self.has_property("morpho_section_fraction_post"):
                # None shan't ever be in the `attribute_names` → defaults to -1.0
                _populate("ipt", _read(None, True))
                _populate("offset", _read("morpho_section_fraction_post", False))
            else:
                _populate("ipt", _read("morpho_segment_id_post", False))
                _populate("offset", _read("morpho_offset_segment_post", False))

        for name in self._extra_fields:
            now_needed_ids = sorted(set(gid for gid in ids if name not in self._data[gid]))
            if needed_ids != now_needed_ids:
                needed_ids = now_needed_ids
                needed_edge_ids = self._population.afferent_edges([gid - 1 for gid in needed_ids])
                tgids = self._population.target_nodes(needed_edge_ids) + 1
            _populate(name, _read(name, False))

    def _load_synapse_parameters(self, gid):
        if gid not in self._data:
            self.preload_data([gid])

        data = self._data[gid]

        if self._extra_fields:
            class CustomSynapseParameters(SynapseParameters):
                _synapse_fields = SynapseParameters._synapse_fields + self._extra_fields
            conn_syn_params = CustomSynapseParameters.create_array(len(data["sgid"]))
        else:
            conn_syn_params = SynapseParameters.create_array(len(data["sgid"]))

        conn_syn_params["sgid"] = data["sgid"]
        for name in SynapseParameters._synapse_fields[1:-2]:
            conn_syn_params[name] = data[name]
        if self._extra_fields:
            for name in self._extra_fields:
                conn_syn_params[name] = data[name]

        return conn_syn_params


class SynReaderNRN(SynapseReader):
    """ Synapse Reader for NRN format only, using the hdf5_reader mod.
    """
    def __init__(self,
                 syn_src, conn_type, population=None,
                 n_synapse_files=None, local_gids=(),  # Specific to NRNReader
                 *_, **kw):
        if os.path.isdir(syn_src):
            filename = "nrn_gj.h5" if conn_type == self.GAP_JUNCTIONS else "nrn.h5"
            syn_src = os.path.join(syn_src, filename)
            log_verbose("Found nrn file: %s", filename)

        # Hdf5 reader doesnt do checks, failing badly (and cryptically) later
        if not os.path.isfile(syn_src) and not os.path.isfile(syn_src + ".1"):
            raise RuntimeError("NRN synapses file not found: " + syn_src)

        # Generic init now that we know the file
        self._n_synapse_files = n_synapse_files or 1  # needed during init
        SynapseReader.__init__(self, syn_src, conn_type, population, **kw)

        if self._n_synapse_files > 1:
            vec = Nd.Vector(len(local_gids))  # excg-location requires true vector
            for num in local_gids:
                vec.append(num)
            self._syn_reader.exchangeSynapseLocations(vec)

    def _open_file(self, syn_src, population, verbose=False):
        if population:
            raise RuntimeError("HDF5Reader doesn't support Populations.")
        log_verbose("Opening synapse file: %s", syn_src)
        self._syn_reader = Nd.HDF5Reader(syn_src, self._n_synapse_files)
        self.nrn_version = self._syn_reader.checkVersion()

    def has_nrrp(self):
        return self.nrn_version > 4

    def has_property(self, field_name):
        logging.warning("has_property() without SynapseReader returns always False")
        return False

    def _load_synapse_parameters(self, gid):
        reader = self._syn_reader
        cell_name = "a%d" % gid

        ret = reader.loadData(gid) if self._n_synapse_files > 1 \
            else reader.loadData(cell_name)

        if ret < 0:  # No dataset
            return SynapseParameters.empty
        nrow = int(reader.numberofrows(cell_name))
        if nrow == 0:
            return SynapseParameters.empty

        conn_syn_params = SynapseParameters.create_array(nrow)
        has_nrrp = self.has_nrrp()

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
            if has_nrrp:
                params[11] = reader.getData(cell_name, i, 17)  # nrrp
            else:
                params[11] = -1

            # placeholder for u_hill_coefficient and conductance_ratio, not supported by HDF5Reader
            params[12] = -1
            params[13] = -1

        return conn_syn_params


class SynToolNotAvail(Exception):
    """Exception thrown when the circuit requires SynapseTool and it is NOT built-in.
    """
    pass
