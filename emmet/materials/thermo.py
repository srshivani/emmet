import logging
from datetime import datetime
from itertools import chain, combinations
from functools import reduce
from collections import defaultdict

from pymatgen import Structure, Composition
from pymatgen.entries.compatibility import MaterialsProjectCompatibility
from pymatgen.entries.computed_entries import ComputedEntry
from pymatgen.analysis.phase_diagram import PhaseDiagram, PhaseDiagramError
from pymatgen.analysis.structure_analyzer import oxide_type, sulfide_type

from maggma.builders import Builder

__author__ = "Shyam Dwaraknath <shyamd@lbl.gov>"


class ThermoBuilder(Builder):
    def __init__(self, materials, thermo, query=None, compatibility=None, **kwargs):
        """
        Calculates thermodynamic quantities for materials from phase
        diagram constructions

        Args:
            materials (Store): Store of materials documents
            thermo (Store): Store of thermodynamic data such as formation
                energy and decomposition pathway
            query (dict): dictionary to limit materials to be analyzed
            compatibility (PymatgenCompatability): Compatability module
                to ensure energies are compatible
        """

        self.materials = materials
        self.thermo = thermo
        self.query = query if query else {}
        self.compatibility = (
            compatibility
            if compatibility
            else MaterialsProjectCompatibility("Advanced")
        )
        self.completed_tasks = set()
        self.entries_cache = defaultdict(list)
        super().__init__(sources=[materials], targets=[thermo], **kwargs)

    def get_items(self):
        """
        Gets sets of entries from chemical systems that need to be processed

        Returns:
            generator of relevant entries from one chemical system
        """
        self.logger.info("Thermo Builder Started")

        self.logger.info("Setting indexes")
        self.ensure_indicies()
        # All relevant materials that have been updated since thermo props were
        # last calculated
        q = dict(self.query)
        q.update(self.materials.lu_filter(self.thermo))
        updated_comps = set(self.materials.distinct("chemsys", q))

        # All materials that are not present in the thermo collection
        thermo_mat_ids = self.thermo.distinct("task_id")
        q = dict(self.query)
        q.update({"task_id": {"$nin": thermo_mat_ids}})
        new_mat_comps = set(self.materials.distinct("chemsys", q))

        # All chemsys not present in thermo collection
        new_comps = set(self.materials.distinct("chemsys", self.query)) - set(
            self.thermo.distinct("chemsys")
        )

        comps = updated_comps | new_comps | new_mat_comps

        # Only process maximal super sets: e.g. if ["A","B"] and ["A"]
        # are both in the list, will only yield ["A","B"] as this will
        # calculate thermo props for all ["A"] compounds
        processed = set()

        to_process = []

        for chemsys in sorted(comps, key=lambda x: len(x.split("-")), reverse=True):
            if chemsys not in processed:
                processed |= chemsys_permutations(chemsys)
                to_process.append(chemsys)

        self.logger.info(
            "Found {} compositions with new/updated materials".format(len(to_process))
        )
        self.total = len(to_process)

        for chemsys in to_process:
            entries = self.get_entries(chemsys)

            # build sandbox sets: ["a"] , ["a","b"], ["core","a","b"]
            sandbox_sets = set(
                [frozenset(entry.data.get("_sbxn", {})) for entry in entries]
            )
            sandbox_sets = maximal_spanning_non_intersecting_subsets(sandbox_sets)
            self.logger.debug(f"Found {len(sandbox_sets)}: {sandbox_sets}")

            for sandboxes in sandbox_sets:
                # only yield maximal subsets so that we can process a equivalent sandbox combinations at a time
                sandbox_entries = [
                    entry
                    for entry in entries
                    if all(
                        sandbox in entry.data.get("_sbxn", []) for sandbox in sandboxes
                    )
                ]

                yield sandboxes, sandbox_entries

    def process_item(self, item):
        """
        Process the list of entries into thermo docs for each sandbox
        Args:
            item (set(entry)): a list of entries to process into a phase diagram

        Returns:
            [dict]: a list of thermo dictionaries to update thermo with
        """

        docs = []

        sandboxes, entries = item
        entries = self.compatibility.process_entries(entries)

        # determine chemsys
        chemsys = "-".join(
            sorted(set([el.symbol for e in entries for el in e.composition.elements]))
        )

        self.logger.debug(
            f"Procesing {len(entries)} entries for {chemsys} - {sandboxes}"
        )

        try:
            pd = PhaseDiagram(entries)

            docs = []

            for e in entries:
                (decomp, ehull) = pd.get_decomp_and_e_above_hull(e)

                d = {
                    self.thermo.key: e.entry_id,
                    "thermo": {
                        "energy": e.uncorrected_energy,
                        "energy_per_atom": e.uncorrected_energy
                        / e.composition.num_atoms,
                        "formation_energy_per_atom": pd.get_form_energy_per_atom(e),
                        "e_above_hull": ehull,
                        "is_stable": e in pd.stable_entries,
                    },
                }

                # Store different info if stable vs decomposes
                if d["thermo"]["is_stable"]:
                    d["thermo"]["eq_reaction_e"] = pd.get_equilibrium_reaction_energy(e)
                else:
                    d["thermo"]["decomposes_to"] = [
                        {
                            "task_id": de.entry_id,
                            "formula": de.composition.formula,
                            "amount": amt,
                        }
                        for de, amt in decomp.items()
                    ]

                d["thermo"]["entry"] = e.as_dict()
                d["thermo"]["explanation"] = self.compatibility.get_explanation_dict(e)

                elsyms = sorted(set([el.symbol for el in e.composition.elements]))
                d["chemsys"] = "-".join(elsyms)
                d["nelements"] = len(elsyms)
                d["elements"] = list(elsyms)
                d["_sbxn"] = list(sandboxes)

                docs.append(d)
        except PhaseDiagramError as p:
            elsyms = []
            for e in entries:
                elsyms.extend([el.symbol for el in e.composition.elements])

            self.logger.warning(
                "Phase diagram errorin chemsys {}: {}".format(
                    "-".join(sorted(set(elsyms))), p
                )
            )
            return []

        return docs

    def update_targets(self, items):
        """
        Inserts the thermo docs into the thermo collection

        Args:
            items ([[dict]]): a list of list of thermo dictionaries to update
        """
        # flatten out lists
        items = list(filter(None, chain.from_iterable(items)))
        # check for duplicates within this set
        items = list(
            {(v[self.thermo.key], frozenset(v["_sbxn"])): v for v in items}.values()
        )
        # Check if already updated this run
        items = [i for i in items if i[self.thermo.key] not in self.completed_tasks]

        self.completed_tasks |= {i[self.thermo.key] for i in items}

        if len(items) > 0:
            self.logger.info("Updating {} thermo documents".format(len(items)))
            self.thermo.update(docs=items, key=[self.thermo.key, "_sbxn"])
        else:
            self.logger.info("No items to update")

    def ensure_indicies(self):
        """
        Ensures indicies on the thermo and materials collections
        :return:
        """
        # Search indicies for materials
        self.materials.ensure_index(self.materials.key, unique=True)
        self.materials.ensure_index(self.materials.lu_field)
        self.materials.ensure_index("chemsys")
        self.materials.ensure_index("_sbxn")

        # Search indicies for thermo
        self.thermo.ensure_index(self.thermo.key)
        self.thermo.ensure_index(self.thermo.lu_field)
        self.thermo.ensure_index("chemsys")
        self.thermo.ensure_index("_sbxn")

    def get_entries(self, chemsys):
        """
        Get all entries in a chemsys from materials

        Args:
            chemsys(str): a chemical system represented by string elements seperated by a dash (-)

        Returns:
            set(ComputedEntry): a set of entries for this system
        """

        self.logger.info("Getting entries for: {}".format(chemsys))

        # First check the cache
        all_chemsys = chemsys_permutations(chemsys)
        cached_chemsys = all_chemsys & set(self.entries_cache.keys())
        query_chemsys = all_chemsys - cached_chemsys

        self.logger.debug(
            "Getting {} entries from cache for {}".format(len(cached_chemsys), chemsys)
        )

        # Query for any chemsys we don't have
        new_q = dict(self.query)
        new_q["chemsys"] = {"$in": list(query_chemsys)}
        new_q["deprecated"] = False

        fields = [
            "structure",
            self.materials.key,
            "thermo.energy_per_atom",
            "composition",
            "calc_settings",
            "_sbxn",
        ]
        data = list(self.materials.query(properties=fields, criteria=new_q))

        # Start with entries from cache
        all_entries = list(
            chain.from_iterable(self.entries_cache[c] for c in cached_chemsys)
        )

        for d in data:
            comp = Composition(d["composition"])
            entry = ComputedEntry(
                comp,
                d["thermo"]["energy_per_atom"] * comp.num_atoms,
                0.0,
                parameters=d["calc_settings"],
                entry_id=d[self.materials.key],
                data={
                    "oxide_type": oxide_type(Structure.from_dict(d["structure"])),
                    "_sbxn": d.get("_sbxn", []),
                },
            )

            # Add to cache
            elsyms = sorted(set([el.symbol for el in comp.elements]))
            self.entries_cache["-".join(elsyms)].append(entry)

            all_entries.append(entry)

        self.logger.info("Total entries in {} : {}".format(chemsys, len(all_entries)))

        return all_entries


def chemsys_permutations(chemsys):
    # Fancy way of getting every unique permutation of elements for all
    # possible number of elements:
    elements = chemsys.split("-")
    return {
        "-".join(sorted(c))
        for c in chain(
            *[combinations(elements, i) for i in range(1, len(elements) + 1)]
        )
    }


def maximal_spanning_non_intersecting_subsets(sets):
    """
    Finds the maximal spanning non intersecting subsets of a group of sets
    This is usefull for parsing out the sandboxes and figuring out how to group
    and calculate these for thermo documents

    sets (set(frozenset)): sets of keys to subsect, expected as a set of frozensets
    """
    to_return_subsets = []

    # Find the overlapping portions and independent portions
    for subset in sets:
        for other_set in sets:
            subset = frozenset(subset.intersection(other_set)) or subset
        if subset:
            to_return_subsets.append(subset)

    # Remove accounted for elements and recurse on remaining sets
    accounted_elements = set(chain.from_iterable(to_return_subsets))
    sets = {frozenset(subset - accounted_elements) for subset in sets}
    sets = {subset for subset in sets if subset}

    if sets:
        to_return_subsets.extend(maximal_spanning_non_intersecting_subsets(sets))

    return set(to_return_subsets)
