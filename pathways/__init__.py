import csv
import logging
import numpy as np
import pulp
import re
import seaborn
import StringIO

from django.utils.text import slugify
from gibbs import constants
from gibbs import service_config
from gibbs.conditions import AqueousParams
from gibbs.reaction import Reaction, CompoundWithCoeff
from matplotlib import pyplot as plt
from os import path
from pathways.bounds import Bounds
from pathways.thermo_models import PathwayThermoModel
from util.SBtab import SBtab


RELPATH = path.dirname(path.realpath(__file__))
COFACTORS_FNAME = path.join(RELPATH, '../pathways/data/cofactors.csv')
DEFAULT_BOUNDS = Bounds.from_csv_filename(
    COFACTORS_FNAME, default_lb=1e-6, default_ub=0.1)
DEFAULT_RT = constants.R * constants.DEFAULT_TEMP


class PathwayParseError(Exception):
    pass


class InvalidReactionFormula(PathwayParseError):
    pass


class UnbalancedReaction(PathwayParseError):
    pass


class ParsedPathway(object):
    """A pathway parsed from user input.
    
    Designed for checking input prior to converting to a stoichiometric model.
    """

    def __init__(self, reactions, fluxes,
                 bounds=None, aq_params=None, model_name=None):
        """Initialize.
        
        Args:
            reactions: a list of gibbs.reaction.Reaction objects.
            fluxes: np.array of relative fluxes in same order as reactions.
        """
        assert len(reactions) == len(fluxes)
        
        self.model_name = model_name
        self.aq_params = aq_params or AqueousParams()
        self.reactions = reactions
        self.reaction_kegg_ids = [r.stored_reaction_id for r in reactions]
        
        self.fluxes = np.array(fluxes)

        # All reactions occur in the same solution/compartment (for now)
        for rxn in self.reactions:
            rxn.aq_params = self.aq_params
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
    def from_csv_file(cls, f,
                      bounds=None, aq_params=None):
        """Returns a pathway parsed from an input file.
        
        Caller responsible for closing f.
        
        Args:
            f: file-like object containing CSV data describing the pathway.
        """
        rxn_matcher = service_config.Get().reaction_matcher
        query_parser = service_config.Get().query_parser
    
        reactions = []
        fluxes = []
    
        for row in csv.DictReader(f):
            rxn_formula = row.get('ReactionFormula')
            if not rxn_formula:
                raise InvalidReactionFormula('Found empty ReactionFormula')

            flux = float(row.get('Flux', 0.0))
            logging.debug('formula = %f x (%s)', flux, rxn_formula)

            # TODO raise errors in this case.
            if not query_parser.IsReactionQuery(rxn_formula):
                raise InvalidReactionFormula("Failed to parse '%s'", rxn_formula)

            parsed = query_parser.ParseReactionQuery(rxn_formula)

            matches = rxn_matcher.MatchReaction(parsed)
            best_match = matches.GetBestMatch()
            rxn = Reaction.FromIds(best_match, fetch_db_names=True)

            # TODO raise errors in this case.
            if not rxn.IsBalanced():
                raise UnbalancedReaction(
                    "ReactionFormula '%s' is not balanced", rxn_formula)
            if not rxn.IsElectronBalanced():
                raise UnbalancedReaction(
                    "ReactionFormula '%s' is not redox balanced", rxn_formula)

            reactions.append(rxn)
            fluxes.append(flux)

        return ParsedPathway(reactions, fluxes, bounds=bounds,
                             aq_params=aq_params)

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
        model = PathwayThermoModel(self.S.T, self.fluxes, dGs,
                                   self.compound_kegg_ids,
                                   self.reaction_kegg_ids,
                                   concentration_bounds=self.bounds)
        return model

    def calc_mdf(self):
        model = self.pathway_model
        mdf = model.mdf_result
        return PathwayMDFData(self, mdf)
        
    def print_reactions(self):
        for f, r in zip(self.fluxes, self.reactions):
            print '%sx %s' % (f, r)

    def to_sbtab(self):
        """Returns a full SBtab description of the model.

        Description includes reaction fluxes and per-compound bounds.
        """
        generic_header_fmt = "!!SBtab TableName='%s' TableType='%s' Document='%s' SBtabVersion='1.0'"
        reaction_header = generic_header_fmt % ('Reaction', 'Reaction', 'Pathway Model')
        reaction_cols = ['!ID', '!ReactionFormula', '!Identifiers:kegg.reaction']

        sio = StringIO.StringIO()
        sio.writelines([reaction_header + '\n'])
        writer = csv.DictWriter(sio, reaction_cols, dialect='excel-tab')

        rxn_ids = []
        for i, rxn in enumerate(self.reactions):
            kegg_id = rxn.stored_reaction_id
            rxn_id = kegg_id
            if rxn.catalyzing_enzymes:
                enz = unicode(rxn.catalyzing_enzymes[0])
                enz_slug = slugify(enz)[:8]
                rxn_id = '%s_%s' % (enz_slug, kegg_id)
            elif not kegg_id:
                rxn_id = 'RXN%03d' % i

            rxn_ids.append(rxn_id)
            d = {'!ID': rxn_id,
                 '!ReactionFormula': str(rxn),
                 '!Identifiers:kegg.reaction': kegg_id}
            writer.writerow(d)

        # Relative fluxes
        flux_header = generic_header_fmt % ('RelativeFlux', 'Quantity', 'Pathway Model')
        flux_cols = ['!QuantityType', '!Reaction', '!Reaction:Identifiers:kegg.reaction', '!Value']
        sio.writelines(['%\n', flux_header + '\n'])
        writer = csv.DictWriter(sio, flux_cols, dialect='excel-tab')

        for i, rxn_id in enumerate(rxn_ids):
            d = {'!QuantityType': 'flux',
                 '!Reaction': rxn_id,
                 '!Reaction:Identifiers:kegg.reaction': self.reactions[i].stored_reaction_id,
                 '!Value': self.fluxes[i]}
            writer.writerow(d)

        conc_header = generic_header_fmt % ('ConcentrationConstraint', 'Quantity', 'Pathway Model')
        conc_header += " Unit='M'"
        conc_cols = ['!QuantityType', '!Compound',
                     '!Compound:Identifiers:kegg.compound',
                     '!Concentration:Min', '!Concentration:Max']
        sio.writelines(['%\n', conc_header + '\n'])

        writer = csv.DictWriter(sio, conc_cols, dialect='excel-tab')
        for cid, compound in self.compounds_by_kegg_id.iteritems():
            d = {'!QuantityType': 'concentration',
                 '!Compound': str(compound.name),
                 '!Compound:Identifiers:kegg.compound': cid,
                 '!Concentration:Min': self.bounds.GetLowerBound(cid),
                 '!Concentration:Max': self.bounds.GetUpperBound(cid)}
            writer.writerow(d)

        return sio.getvalue()



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

    @staticmethod
    def html_conc(conc):
        if conc <= 9.999e-4:
            return '%.1f &mu;M' % (1e6*conc)
        return '%.1f mM' % (1e3*conc)

    @property
    def html_concentration(self):
        return self.html_conc(self.concentration)

    @property
    def html_lb(self):
        return self.html_conc(self.lb)

    @property
    def html_ub(self):
        return self.html_conc(self.ub)   


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
        cbounds = [self.model.concentration_bounds.GetBoundTuple(cid)
                   for cid in parsed_pathway.compound_kegg_ids]
        concs = self.mdf_result.concentrations.flatten().tolist()[0]
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

    @property
    def max_total_driving_force(self):
        return -self.min_total_dG

    @property
    def min_total_driving_force(self):
        return -self.max_total_dG

    @property
    def conc_plot_svg(self):
        ys = np.arange(0, len(self.compound_data))
        concs = [c.concentration for c in self.compound_data]
        cnames = [str(c.compound) for c in self.compound_data]
        default_lb = self.model.concentration_bounds.default_lb
        default_ub = self.model.concentration_bounds.default_ub

        conc_figure = plt.figure(figsize=(8, 6))
        seaborn.set_style('darkgrid')
        plt.axes([0.2, 0.1, 0.9, 0.9])
        plt.axvspan(1e-8, default_lb, color='y', alpha=0.5)
        plt.axvspan(default_ub, 1e3, color='y', alpha=0.5)
        plt.scatter(concs, ys, figure=conc_figure)

        plt.xticks(family='sans-serif', figure=conc_figure)
        plt.yticks(ys, cnames, family='sans-serif',
            fontsize=6, figure=conc_figure)
        plt.xlabel('Concentration (M)', family='sans-serif',
            figure=conc_figure)
        plt.xscale('log')

        plt.xlim(1e-7, 1.5e2)
        plt.ylim(-1.5, len(self.compound_data) + 0.5)

        svg_data = StringIO.StringIO()
        conc_figure.savefig(svg_data, format='svg')
        return svg_data.getvalue()

    @property
    def mdf_plot_svg(self):
        dgs = [0] + [r.dGr for r in self.reaction_data]
        dg0s = [0] + [r.dG0_prime for r in self.reaction_data]
        cumulative_dgs = np.cumsum(dgs)
        cumulative_dg0s = np.cumsum(dg0s)

        xs = np.arange(0, len(cumulative_dgs))

        mdf_fig = plt.figure(figsize=(8, 8))
        seaborn.set_style('darkgrid')
        plt.plot(xs, cumulative_dg0s, label='Standard concentrations')
        plt.plot(xs, cumulative_dgs, label='MDF optimized concentrations')
        plt.xticks(xs, family='sans-serif')
        plt.yticks(family='sans-serif')
        
        plt.xlabel('After Reaction Step', family='sans-serif')
        plt.ylabel("Cumulative $\Delta_r G'$ (kJ/mol)", family='sans-serif')
        plt.legend(loc=3)

        svg_data = StringIO.StringIO()
        mdf_fig.savefig(svg_data, format='svg')
        return svg_data.getvalue()
