import logging
from datetime import datetime

from monty.json import jsanitize
from monty.tempfile import ScratchDir
from pymatgen.core.structure import Structure
from pymatgen.electronic_structure.boltztrap import BoltztrapRunner

from maggma.builders import Builder

__author__ = "Shyam Dwaraknath <shyamd@lbl.gov>"


class BoltztrapDosBuilder(Builder):
    def __init__(self,
                 materials,
                 boltztrap,
                 bandstructure_fs="bandstructure_fs",
                 btz_cdos_fs=None,
                 query=None,
                 **kwargs):
        """
        Calculates Density of States (DOS) using BoltzTrap
        Saves the dos object

        Args:
            materials (Store): Store of materials documents
            boltztrap (Store): Store of boltztrap
            bandstructure_fs (str): Name of the GridFS where bandstructures are stored
            query (dict): dictionary to limit materials to be analyzed

        """

        self.materials = materials
        self.boltztrap = boltztrap
        self.bandstructure_fs = bandstructure_fs
        self.btz_cdos_fs = btz_cdos_fs
        self.query = query if query else {}

        super().__init__(sources=[materials], targets=[boltztrap], **kwargs)

    def get_items(self):
        """
        Gets all materials that need a new DOS

        Returns:
            generator of materials to calculate DOS
        """

        self.logger.info("BoltzTrap Dos Builder Started")

        # All relevant materials that have been updated since boltztrap was last run
        # and a uniform bandstructure exists
        q = dict(self.query)
        q.update(self.materials.lu_filter(self.boltztrap))
        q["bandstructure.uniform_oid"] = {"$exists": 1}
        #q["output.bandgap"] = {"$gt": 0.0}
        mats = set(self.materials.distinct(self.materials.key, criteria=q))

        # initialize the gridfs
        bfs = gridfs.GridFS(self.materials.database, self.bandstructure_fs)

        self.logger.info("Found {} new materials for calculating boltztrap dos".format(len(mats)))
        for m in mats:
            mat = self.materials.query([self.materials.key, "structure", "input.parameters.NELECT", "bandstructure"],
                                       criteria={self.materials.key: m})

            # If a bandstructure oid exists
            if "uniform_bs_oid" in mat.get("bandstructure", {}):
                bs_json = bfs.get(mat["bandstructure"]["uniform_bs_oid"]).read()

                if "zlib" in mat["bandstructure"].get("uniform_bs_compression", ""):
                    bs_json = zlib.decompress(bs_json)

                bs_dict = json.loads(bs_json.decode())
                mat["bandstructure"]["uniform_bs"] = bs_dict

            yield mat

    def process_item(self, item):
        """
        Calculates dos running Boltztrap in DOS run mode

        Args:
            item (dict): a dict with a material_id, bs and a structure

        Returns:
            cdos: a complete dos object
        """
        self.logger.debug("Calculating Boltztrap for {}".format(item[self.materials.key]))

        nelect = item["input"]["parameters"]["NELECT"]

        bs_dict = item["uniform_bandstructure"]["bs"]
        bs_dict['structure'] = item['structure']
        bs = BandStructure.from_dict(bs_dict)

        with ScratchDir("."):
            if bs.is_spin_polarized:
                run_path = os.path.join(os.getcwd(), "dos_up")
                makedirs_p(run_path)
                BoltztrapRunner(bs=bs, nelec=nelect, run_type="DOS", dos_type="TETRA", spin=1).run(path_dir=run_path)
                btrap_dir = os.path.join(run_path, "boltztrap")
                bta_up = BoltztrapAnalyzer.from_files(btrap_dir)

                run_path = os.path.join(os.getcwd(), "dos_dw")
                makedirs_p(run_path)
                BoltztrapRunner(bs=bs, nelec=nelect, run_type="DOS", dos_type="TETRA", spin=-1).run(path_dir=run_path)
                btrap_dir = os.path.join(run_path, "boltztrap")
                bta_dw = BoltztrapAnalyzer.from_files(btrap_dir)

                cdos = an_up.get_complete_dos(bs.structure, an_dw)

            else:
                run_path = os.path.join(os.getcwd(), "dos")
                makedirs_p(run_path)
                BoltztrapRunner(bs=bs, nelec=nelect, run_type="DOS", dos_type="TETRA").run(path_dir=run_path)
                btrap_dir = os.path.join(run_path, "boltztrap")
                bta = BoltztrapAnalyzer.from_files(btrap_dir)

                cdos = an.get_complete_dos(bs.structure)

        return {'cdos': cdos.as_dict()}

    def update_targets(self, items):
        """
        Inserts the new task_types into the task_types collection

        Args:
            items ([[dict]]): a list of list of thermo dictionaries to update
        """
        items = list(filter(None, items))

        btz_cdos_fs = gridfs.GridFS(self.materials.database, self.btz_cdos_fs) if self.btz_cdos_fs else None

        if len(items) > 0:
            self.logger.info("Updating {} boltztrap dos".format(len(items)))

            for doc in items:
                if self.bta_fs:
                    btz_dos_doc = dict(doc["cdos"])
                    btz_dos_json = json.dumps(jsanitize(btz_dos_doc))
                    btz_dos_gz = zlib.compress(btz_dos_json)
                    btz_dos_oid = btz_dos_fs.put(btz_dos_gz)
                    doc['btz_dos_oid'] = btz_dos_oid
                    doc['btz_dos_compression'] = "zlib"

                del doc["cdos"]

            self.boltztrap.update(items)

        else:
            self.logger.info("No items to update")


class BoltztrapBuilder(Builder):
    def __init__(self, materials, boltztrap, bandstructure_fs="bandstructure_fs", bta_fs=None, query=None, **kwargs):
        """
        Calculates conducitivty parameters using BoltzTrap
        Saves the boltztrap analyzer in bta_fs if set otherwise doesn't store it
        because it is too large usually to store in Mongo

        Args:
            materials (Store): Store of materials documents
            boltztrap (Store): Store of boltztrap
            bandstructure_fs (str): Name of the GridFS where bandstructures are stored
            query (dict): dictionary to limit materials to be analyzed

        """

        self.materials = materials
        self.boltztrap = boltztrap
        self.bandstructure_fs = bandstructure_fs
        self.bta_fs = bta_fs
        self.query = query if query else {}

        super().__init__(sources=[materials], targets=[boltztrap], **kwargs)

    def get_items(self):
        """
        Gets all materials that need a new XRD

        Returns:
            generator of materials to calculate xrd
        """

        self.logger.info("BoltzTrap Builder Started")

        # All relevant materials that have been updated since boltztrap was last run
        # and a uniform bandstructure exists
        q = dict(self.query)
        q.update(self.materials.lu_filter(self.boltztrap))
        q["bandstructure.uniform_oid"] = {"$exists": 1}
        q["output.bandgap"] = {"$gt": 0.0}
        mats = set(self.materials.distinct(self.materials.key, criteria=q))

        # initialize the gridfs
        bfs = gridfs.GridFS(self.materials.database, self.bandstructure_fs)

        self.logger.info("Found {} new materials for calculating boltztrap conductivity".format(len(mats)))
        for m in mats:
            mat = self.materials.query([self.materials.key, "structure", "input.parameters.NELECT", "bandstructure"],
                                       criteria={self.materials.key: m})

            # If a bandstructure oid exists
            if "uniform_bs_oid" in mat.get("bandstructure", {}):
                bs_json = bfs.get(mat["bandstructure"]["uniform_bs_oid"]).read()

                if "zlib" in mat["bandstructure"].get("uniform_bs_compression", ""):
                    bs_json = zlib.decompress(bs_json)

                bs_dict = json.loads(bs_json.decode())
                mat["bandstructure"]["uniform_bs"] = bs_dict

            yield mat

    def process_item(self, item):
        """
        Calculates diffraction patterns for the structures

        Args:
            item (dict): a dict with a material_id and a structure

        Returns:
            dict: a diffraction dict
        """
        self.logger.debug("Calculating Boltztrap for {}".format(item[self.materials.key]))

        nelect = item["input"]["parameters"]["NELECT"]

        bs_dict = item["uniform_bandstructure"]["bs"]
        bs_dict['structure'] = item['structure']
        bs = BandStructure.from_dict(bs_dict)

        with ScratchDir("."):
            BoltztrapRunner(bs=bs, nelec=nelect).run(path_dir=os.getcwd())
            btrap_dir = os.path.join(os.getcwd(), "boltztrap")
            bta = BoltztrapAnalyzer.from_files(btrap_dir)

        d = {
            "bta": bta.as_dict(),
            "boltztrap": {
                "thermoelectric": bt_analysis_thermoelectric(bta),
                "tcm": bt_analysis_tcm(bta)
            }
        }

        return d

    def update_targets(self, items):
        """
        Inserts the new task_types into the task_types collection

        Args:
            items ([[dict]]): a list of list of thermo dictionaries to update
        """
        items = list(filter(None, items))

        bta_fs = gridfs.GridFS(self.materials.database, self.bta_fs) if self.bta_fs else None

        if len(items) > 0:
            self.logger.info("Updating {} boltztrap docs".format(len(items)))

            for doc in items:
                if self.bta_fs:
                    bta_doc = dict(doc["bta"])
                    bta_json = json.dumps(jsanitize(bta_doc))
                    bta_gz = zlib.compress(bta_json)
                    bta_oid = bta_fs.put(bta_gz)
                    doc['bta_oid'] = bta_oid
                    doc['bta_compression'] = "zlib"

                del doc["bta"]

            self.boltztrap.update(items)

        else:
            self.logger.info("No items to update")


def bt_analysis_thermoelectric(bta):
    """
    Performs analysis for thermoelectrics search
    :param bta: Boltztrap analyzer object
    :return: dict of Zt,Power Factor, Seebeck, Conducitity and Kappa
    """
    d = {}

    d["zt"] = bta.get_extreme("zt")
    d["pf"] = bta.get_extreme("power factor")
    d["seebeck"] = bta.get_extreme("seebeck")
    d["conductivity"] = bta.get_extreme("conductivity")
    d["kappa_max"] = bta.get_extreme("kappa")
    d["kappa_min"] = bta.get_extreme("kappa", maximize=False)

    return d


def bt_analysis_tcm(bta, temp_min=300, temp_max=400, doping_min=1e19, doping_max=1e22):
    """
    Performs analysis for transparent conductive materials
    Focuses on T=300-400K and Doping=1E19-1E22
    :param bta: Boltztrap analyzer object
    :return: dict of conductivity and effective mass
    """
    d = {}

    d['avg_eff_mass'] = bta.get_average_eff_mass()
    d['doping'] = bta.doping

    return d
