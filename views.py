import base64
import logging
import json
import io
import os
from django.http import HttpResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import render
from django.http import Http404
from django.template.context_processors import csrf
from django.apps import apps
from settings import STATIC_URL
from gibbs import reaction, conditions, constants, service_config, \
                  pathway_result_page
from gibbs.forms import CompoundForm, EnzymeForm, ReactionForm, \
                        ReactionGraphForm, SearchForm, SuggestForm, \
                        AnalyzePathwayModelForm, BuildPathwayModelForm
from pathways import ParsedPathway, PathwayParseError
import util.django_utils

NO_STRUCTURE_THUMBNAIL = os.path.join([STATIC_URL, 'images',
                                       'structure_not_available.png'])


def MainPage(request):
    """Renders the landing page."""
    return render(request, 'main.html', {})


def AboutPage(request):
    """Renders the about page."""
    return render(request, 'about.html', {})


def FAQPage(request):
    """Renders the FAQ page."""
    return render(request, 'faq.html', {})


def WhatsNewPage(request):
    """Renders the FAQ page."""
    return render(request, 'new_in_2_0.html', {})


def CitePage(request):
    """Renders the FAQ page."""
    return render(request, 'cite.html', {})


def ClassicReactions(request):
    """Renders the classic reactions page."""
    return render(request, 'classic_reactions.html', {})


def DownloadPage(request):
    """Renders the download page."""
    ph_values = map(lambda x: '%.1f' % x, constants.PH_RANGE_VALUES)
    return render(request, 'download.html', {'ph_values': ph_values})


def RefsPage(request):
    """Renders a list of relevant literature."""
    sources = apps.get_model('gibbs.ValueSource').objects.all()
    sorted_sources = sorted(sources, key=lambda s: s.citation)
    template_data = {"sources": sorted_sources}
    return render(request, 'data_refs.html', template_data)


def Robots(request):
    """Renders robots.txt."""
    return render(request, 'robots.txt', {})


@csrf_exempt
def CompoundImage(request):
    data = request.GET
    compound_id = data.get('compoundId', None)
    if compound_id is None:
        return HttpResponseBadRequest('No request data.')
    compounds = apps.get_model('gibbs.Compound').objects.filter(
        kegg_id=compound_id)
    if not compounds:
        return HttpResponseBadRequest('No such compound.')
    compound = compounds[0]
    if not compound.thumbnail or compound.thumbnail == 'error':
        image_data = open(NO_STRUCTURE_THUMBNAIL, 'r').read()
    else:
        image_data = base64.decodestring(compound.thumbnail)
    return HttpResponse(image_data, content_type='image/png')


def CompoundPage(request):
    """Renders a page for a particular compound."""
    form = CompoundForm(request.GET)
    if not form.is_valid():
        logging.error(form.errors)
        raise Http404
    aq_params = conditions.AqueousParams.FromForm(form, request.COOKIES)
    rxn = reaction.Reaction.FromForm(form, aq_params)
    if len(rxn.reactants) != 1:
        logging.error('There must be only 1 reactant in a "compound" page')
        raise Http404

    compound = rxn.reactants[0].compound
    logging.info('Submit = ' + form.cleaned_submit)

    if form.cleaned_submit == 'Reset':
        logging.info('resetting conditions')
        rxn.aq_params = conditions.AqueousParams()
        rxn.ResetConcentrations()

    compound.StashTransformedSpeciesEnergies(rxn.aq_params)

    query = compound.FirstName()
    template_data = rxn.GetTemplateData(query)
    template_data.update({'compound': compound,
                          'alberty_link': compound.GetNewPriorityLink(99),
                          'cc_link': compound.GetNewPriorityLink(1)})
    response = render(request, 'compound_page.html', template_data)
    rxn.aq_params.SetCookies(response)
    return response


def EnzymePage(request):
    """Renders a page for a particular enzyme."""
    form = EnzymeForm(request.GET)
    if not form.is_valid():
        logging.error(form.errors)
        raise Http404

    enz = apps.get_model('gibbs.Enzyme').objects.get(ec=form.cleaned_ec)
    template_data = {'is_superuser': util.django_utils.IsSuperUser(request),
                     'enzyme': enz}
    return render(request, 'enzyme_page.html', template_data)

_REACTION_TEMPLATES_BY_SUBMIT = {'': 'reaction_page.html',
                                 'Update': 'reaction_page.html',
                                 'Save': 'print_reaction.html',
                                 'Reverse': 'reaction_page.html',
                                 'Reset': 'reaction_page.html'}


def ReactionPage(request):
    """Renders a page for a particular reaction."""
    form = ReactionForm(request.GET)
    if not form.is_valid():
        logging.error(form.errors)
        return HttpResponseBadRequest('Invalid reaction form.')
    aq_params = conditions.AqueousParams.FromForm(form, request.COOKIES)
    rxn = reaction.Reaction.FromForm(form, aq_params)

    if form.cleaned_submit == 'Reverse':
        rxn.SwapSides()
    elif form.cleaned_submit == 'Reset':
        rxn.aq_params = conditions.AqueousParams()
        rxn.ResetConcentrations()
    query = rxn.GetQueryString()
    # Render the template.
    if form.cleaned_submit not in _REACTION_TEMPLATES_BY_SUBMIT:
        logging.error('Unknown submit term for reaction page: ' +
                      form.cleaned_submit)
        raise Http404
    template_name = _REACTION_TEMPLATES_BY_SUBMIT[form.cleaned_submit]
    response = render(request, template_name, rxn.GetTemplateData(query))
    rxn.aq_params.SetCookies(response)
    return response


def ReactionGraph(request):
    """Renders the graph page."""
    form = ReactionGraphForm(request.GET)
    if not form.is_valid():
        logging.error(form.errors)
        raise Http404

    mode = 'varyPh'
    if form.cleaned_vary_is:
        mode = 'varyIs'

    aq_params = conditions.AqueousParams.FromForm(form, request.COOKIES)
    rxn = reaction.Reaction.FromForm(form, aq_params)

    compound_list = [c.GetCompoundList() for c in rxn.reactants]
    coeff_list = [c.coeff for c in rxn.reactants]
    concentration_list = [c.phase.Value() for c in rxn.reactants]
    template_data = {'compound_data': json.dumps(compound_list),
                     'coeff_data': json.dumps(coeff_list),
                     'concentration_list': json.dumps(concentration_list),
                     'mode': mode,
                     'reaction': rxn,
                     'query': rxn.GetQueryString()}
    template_data.update(rxn.aq_params.GetTemplateData())
    return render(request, 'reaction_graph.html', template_data)


def ResultsPage(request):
    """Renders the search results page for a given query."""
    form = SearchForm(request.GET)
    if not form.is_valid():
        raise Http404
    logging.debug('Generating a search result page')
    query_parser = service_config.Get().query_parser
    reaction_matcher = service_config.Get().reaction_matcher
    matcher = service_config.Get().search_matcher
    query = form.cleaned_query
    logging.debug('Query: "%s"', query)
    if not query.strip():
        response = render(request, 'main.html', {})
        return response
    # Check if we should parse and process the input as a reaction.
    if query_parser.IsReactionQuery(query):
        logging.info('Parsing the query as a reaction')
        try:
            parsed_reaction = query_parser.ParseReactionQuery(query)
        except Exception:
            return render(request, 'parse_error_page.html')

        reaction_matches = reaction_matcher.MatchReaction(parsed_reaction)
        best_reaction = reaction_matches.GetBestMatch()
        if not best_reaction:
            return render(request, 'search_error_page.html', {'query': query})

        logging.debug('Generating a reaction from the matched KEGG IDs')
        aq_params = conditions.AqueousParams.FromForm(form, request.COOKIES)
        rxn = reaction.Reaction.FromIds(best_reaction, aq_params)
        response = render(request, 'reaction_page.html',
                          rxn.GetTemplateData(query))
        return response

    else:
        # Otherwise we try to parse it as a single compound.
        logging.debug('Parsing the query as a single compound/enzyme')
        results = matcher.Match(query)
        template_data = {}
        template_data['compound_results'] = \
            [m for m in results if m.IsCompound()]
        template_data['enzyme_results'] = [m for m in results if m.IsEnzyme()]
        template_data['enzymes_first'] = results and results[0].IsEnzyme()
        template_data['query'] = query
        response = render(request, 'search_results.html', template_data)
        return response

    raise Http404


def SuggestJson(request):
    """Renders the suggest JSON."""
    form = SuggestForm(request.GET)
    if not form.is_valid():
        logging.error(form.errors)
        raise Http404
    query = str(form.cleaned_query)
    suggestions = []
    if query:
        matcher = service_config.Get().search_matcher
        matches = matcher.Match(query)
        suggestions = [{'value': str(m.key), 'data': {'cat': m.key.TypeStr()}}
                       for m in matches]
    output = {'query': query,
              'suggestions': suggestions}
    json_data = json.dumps(output)
    return HttpResponse(json_data, content_type='application/json')


def DefinePathwayPage(request):
    """Renders the landing page."""
    template_params = csrf(request)
    template_params['constants'] = constants
    return render(request, 'define_pathway_page.html', template_params)


def PathwayResultPage(request):
    """Renders a page for a particular reaction."""
    form = AnalyzePathwayModelForm(request.POST, request.FILES)
    if not form.is_valid():
        logging.error(form.errors)
        return HttpResponseBadRequest('Invalid pathway form.')

    try:
        f_data = unicode(request.FILES['pathway_file'].read())
        sio = io.StringIO(f_data, newline=None)  # universal newline mode
        reactions, fluxes, keqs, bounds = pathway_result_page.read_sbtabs(sio)
        pp = ParsedPathway.from_full_sbtab(
            reactions, fluxes, bounds, keqs)
        logging.info('Parsed pathway.')
    except PathwayParseError as ppe:
        logging.error(ppe)
        return HttpResponseBadRequest(ppe.message)

    try:
        # calculate the MDF with the specified bounds. Render template.
        mdf_result = pp.calc_mdf()
        template_data = {'pathway': pp,
                         'mdf_result': mdf_result}
        logging.info('Calculated MDF %s', mdf_result.mdf)
    except Exception as e:
        logging.error(e)
        return HttpResponseBadRequest(e.message)
    return render(request, 'pathway_result_page.html', template_data)


def BuildPathwayModel(request):
    """Renders a page for a particular reaction."""
    form = BuildPathwayModelForm(request.POST, request.FILES)
    if not form.is_valid():
        logging.error(form.errors)
        return HttpResponseBadRequest('Invalid pathway form.')

    try:
        bounds = pathway_result_page.make_bounds(form)
        aq_params = pathway_result_page.make_aq_params(form)
    except Exception as e:
        logging.error(e)
        return HttpResponseBadRequest(e)

    try:
        f = request.FILES['pathway_file']
        fname_base, ext = os.path.splitext(f.name)
        output_fname = '%s_pH%.2f_I%.2f.tsv' % (
            fname_base, aq_params.pH, aq_params.ionic_strength)
        logging.info(output_fname)

        f_data = unicode(f.read())
        sio = io.StringIO(f_data, newline=None)  # universal newline mode
        pp = ParsedPathway.from_csv_file(
            sio, bounds=bounds, aq_params=aq_params)
    except PathwayParseError as ppe:
        logging.error(ppe)
        return HttpResponseBadRequest(ppe)

    response = HttpResponse(content_type='text/tab-separated-values')
    response['Content-Disposition'] = 'attachment; filename="%s"' % \
        output_fname
    response.write(pp.to_full_sbtab())

    return response
