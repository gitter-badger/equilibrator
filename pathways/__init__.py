import csv
import logging
import numpy as np
import pulp
import re

from gibbs import constants
from gibbs.reaction import Reaction, CompoundWithCoeff
from matching import query_parser
from pathways.bounds import Bounds
from pathways.thermo_models import KeggPathwayModel


DEFAULT_BOUNDS = Bounds.from_csv_filename('pathways/data/cofactors.csv')
DEFAULT_RT = constants.R * constants.DEFAULT_TEMP


class ParsedPathway(object):
    """A pathway parsed from user input.
    
    Designed for checking input prior to converting to a stoichiometric model.
    """

    def __init__(self, reactions, fluxes, bounds=None):
        """Initialize.
        
        Args:
            reactions: a list of gibbs.reaction.Reaction objects.
            fluxes: np.array of relative fluxes in same order as reactions.
        """
        assert len(reactions) == len(fluxes)
        
        self.reactions = reactions
        self.reaction_kegg_ids = [r.stored_reaction_id for r in reactions]
        
        self.fluxes = np.array(fluxes)
        self.dG0_r_prime = [r.DeltaG0Prime() for r in reactions]

        self.bounds = bounds or DEFAULT_BOUNDS

        self.S, self.compound_kegg_ids = self._build_stoichiometric_matrix()
        self.compounds_by_kegg_id = self._get_compounds()
        self.compounds = [self.compounds_by_kegg_id[cid]
                          for cid in self.compound_kegg_ids]

        nr, nc = self.S.shape
        net_rxn_stoich = (self.fluxes.reshape((nr, 1)) * self.S).sum(axis=0)
        net_rxn_data = []
        for coeff, kid in zip(net_rxn_stoich, self.compound_kegg_ids):
            if coeff != 0:
                net_rxn_data.append(self._reactant_dict(coeff, kid))
        self.net_reaction = Reaction.FromIds(net_rxn_data, fetch_db_names=True)
        self._model = self.pathway_model
            
    @staticmethod
    def _reactant_dict(coeff, kid, negate=False):
        """Returns dictionary format expected by Reaction.FromIds."""
        if negate:
            coeff = -1*coeff
        d = {'kegg_id': kid, 'coeff': coeff, 'name': kid,
             'phase': constants.AQUEOUS_PHASE_NAME}
        if kid == 'C00001':
            # Water is not aqueous. Hate that this is hardcoded.
            d['phase'] = constants.LIQUID_PHASE_NAME
        return d
    
    @classmethod
    def from_file(cls, f):
        """Returns a pathway parsed from an input file.
        
        Caller responsible for closing f.
        
        Args:
            f: file-like object containing CSV data describing the pathway
                reactions and relative fluxes using KEGG IDs to identify compounds.
        """
        side_parser = query_parser._MakeReactionSideParser()
    
        reactions = []
        fluxes = []
    
        for row in csv.DictReader(f):
            substrates = row.get('SUBSTRATES')
            products = row.get('PRODUCTS')
            assert substrates, 'Reaction must have substrates.'
            assert products, 'Reaction must have products.'
            flux = row.get('FLUX', 0.0)
            
            fluxes.append(float(flux))
            parsed_substrates = side_parser.parseString(substrates)[0]
            parsed_products = side_parser.parseString(products)[0]
            
            # KEGG ID as name, negate coefficients for substrates.
            parsed_substrates = [cls._reactant_dict(coeff, kid, negate=True)
                                 for coeff, kid in parsed_substrates]
            parsed_products = [cls._reactant_dict(coeff, kid, negate=False)
                               for coeff, kid in parsed_products]
            
            compound_list = parsed_substrates + parsed_products
            rxn = Reaction.FromIds(compound_list, fetch_db_names=True)
            reactions.append(rxn)
            
        return ParsedPathway(reactions, fluxes)
        
    @classmethod
    def from_filename(cls, fname):
        """Returns a pathway parsed from an input file.
        
        Args:
            filename: filename of CSV data describing the pathway
                reactions and relative fluxes using KEGG IDs to identify compounds.
        """
        with open(fname, 'rU') as f:
            return cls.from_file(f)

    def _get_compounds(self):
        """Returns a dictionary of compounds by KEGG ID."""
        compounds = {}
        for r in self.reactions:
            for cw_coeff in r.reactants:
                c = cw_coeff.compound
                compounds[c.kegg_id] = c
        return compounds

    def _build_stoichiometric_matrix(self):
        """Builds a stoichiometric matrix.
        
        Returns:
            Two tuple (S, compounds) where compounds is the KEGG IDs of the compounds
            in the order defining the column order of the stoichiometric matrix S.
        """
        compounds = []
        sparses = []
        for r in self.reactions:
            s = r.GetSparseRepresentation()
            sparses.append(s)
            for kegg_id in s:
                compounds.append(kegg_id)
        compounds = sorted(set(compounds))
        
        # reactions on the rows, compounds on the columns
        n_reactions = len(self.reactions)
        n_compounds = len(compounds)
        smat = np.zeros((n_reactions, n_compounds))
        for i, s in enumerate(sparses):
            for j, c in enumerate(compounds):
                smat[i, j] = s.get(c, 0)
        return smat, compounds
    
    @property
    def reactions_balanced(self):
        """Returns true if all pathway reactions are electron and atom-wise balanced."""
        atom_balaned = [r.IsBalanced() for r in self.reactions]
        electron_balaned = [r.IsElectronBalanced() for r in self.reactions]
        
        balanced = np.logical_and(atom_balaned, electron_balaned)
        return np.all(balanced)
    
    @property
    def reactions_have_dG(self):
        return np.all([dG is not None for dG in self.dG0_r_prime])
    
    @property
    def pathway_model(self):
        dGs = np.matrix(self.dG0_r_prime).T
        bounds_dict = self.bounds.GetOldStyleBounds(self.compound_kegg_ids)
        model = KeggPathwayModel(self.S.T, dGs,
                                 self.compound_kegg_ids,
                                 self.reaction_kegg_ids,
                                 fluxes=self.fluxes,
                                 cid2bounds=bounds_dict)
        return model

    def calc_mdf(self):
        model = self.pathway_model
        mdf = model.mdf_result
        return PathwayMDFData(self, mdf)
        
    def print_reactions(self):
        for f, r in zip(self.fluxes, self.reactions):
            print '%sx %s' % (f, r)


class ReactionMDFData(object):

    def __init__(self, reaction, flux, dGr, shadow_price):
        self.reaction = reaction
        self.flux = flux
        self.dGr = dGr
        self.shadow_price = shadow_price

    @property
    def dG0_prime(self):
        return self.reaction.dg0_prime


class CompoundMDFData(object):
    def __init__(self, compound, concentration_bounds,
                 concentration, shadow_price):
        self.compound = compound
        self.concentration = concentration
        self.shadow_price = shadow_price
        self.lb, self.ub = concentration_bounds

    @property
    def link_url(self):
        return '/compound?compoundId=%s' % self.compound.kegg_id

    @property
    def bounds_equal(self):
        return self.lb == self.ub


class PathwayMDFData(object):

    def __init__(self, parsed_pathway, mdf_result):
        self.parsed_pathway = parsed_pathway
        self.mdf_result = mdf_result
        self.model = mdf_result.model

        rxns = parsed_pathway.reactions
        fluxes = parsed_pathway.fluxes
        dGs = self.mdf_result.dG_r_prime_adj.flatten().tolist()[0]
        prices = self.mdf_result.reaction_prices.flatten().tolist()[0]
        self.reaction_data = [ReactionMDFData(*t) for t in zip(rxns, fluxes, dGs, prices)]

        compounds = parsed_pathway.compounds
        cbounds = [self.model.GetConcentrationBounds(cid)
                   for cid in parsed_pathway.compound_kegg_ids]
        concs = self.mdf_result.concentrations
        prices = self.mdf_result.compound_prices.flatten().tolist()[0]
        
        self.compound_data = [CompoundMDFData(*t) for t in zip(compounds, cbounds, concs, prices)]

    @property
    def mdf(self):
        return self.mdf_result.mdf

    @property
    def min_total_dG(self):
        return self.mdf_result.min_total_dG

    @property
    def max_total_dG(self):
        return self.mdf_result.max_total_dG