import operator
import numpy as np

import urllib
import urllib.parse as up

import nltk
import wikitextparser as wtp
import mwparserfromhell
from mwparserfromhell.nodes.text import Text
from mwparserfromhell.nodes.wikilink import Wikilink 
import re

import nltk
from nltk.util import ngrams
######################
## parsing titles
######################

def normalise_title(title):
    """ 
    Normalising title (links)
    - deal with quotes
    - strip()
    - '_'--> ' '
    - capitalize first letter
    """
    title = up.unquote(title)
    title = title.strip()
    if len(title) > 0:
        title = title[0].upper() + title[1:]
    n_title = title.replace("_", " ")
    if '#' in n_title:
        n_title = n_title.split('#')[0]
    return n_title

def normalise_anchor(anchor):
    '''
    Normalising anchor  (text):
    - strip()
    - lowercase
    Note that we do not do the other normalisations since we want to match the strings from the text
    '''
    # anchor = up.unquote(anchor)
    n_anchor = anchor.strip()#.replace("_", " ")
    return n_anchor.lower()


def wtpGetLinkAnchor(wikilink):
    '''
    extract anchor and link from a wikilink from wikitextparser.
    normalise title and anchor
    '''
    ## normalise the article title (quote, first letter capital) 
    link_tmp = wikilink.title
    link = normalise_title(link_tmp)
    ## normalise the anchor text (strip and lowercase) 
    anchor_tmp = wikilink.text if wikilink.text else link_tmp
    anchor = normalise_anchor(anchor_tmp)
    return link, anchor




def getLinks(wikicode, redirects=None, pageids=None):
    '''
    get all links in a page
    '''
    link_dict={}
    linklist = wtp.parse(str(wikicode)).wikilinks
    for l in linklist:
        link,anchor = wtpGetLinkAnchor(l)
        ## if redirects is not None, resolve the redirect
        if redirects != None:
            link = resolveRedirect(link,redirects)
        ## if pageids is not None, keep only links appearing as key in pageids
        if pageids !=None:
            if link not in pageids:
                continue
        link_dict[anchor] = link
    return link_dict

def resolveRedirect(link, redirects):
    '''
    resolve the redirect.
    check whether in pageids (main namespace)

    '''
    return redirects.get(link,link)

# Split a MWPFH node <TEXT> into sentences
def tokenizeSent(text):
    SENT_ENDS = [u".", u"!", u"?"]
    for line in text.split("\n"):
        tok_acc = []
        for tok in nltk.word_tokenize(line):
            tok_acc.append(tok)
            if tok in SENT_ENDS:
                yield " ".join(tok_acc)
                tok_acc = []
        if tok_acc:
            yield " ".join(tok_acc)


##########################
## getting the wikitexct from the API
##########################
import requests
def getWikitext(title, lang):
    '''
    get the wikitext for a pagetitile for a lang
    '''
    params = {
        "action": "query",
        "prop": "revisions",
        "rvprop": "content",
        "rvslots": "main",
        "rvlimit": 1,
        "titles": title,
        "format": "json",
        "formatversion": "2",
    }
    API_URL = "https://{0}.wikipedia.org/w/api.php".format(lang)
    headers = {"User-Agent": "My-Bot-Name/1.0"}
    req = requests.get(API_URL, headers=headers, params=params)
    res = req.json()
    revision = res["query"]["pages"][0]["revisions"][0]
    wikitext = revision["slots"]["main"]["content"]
    return wikitext

def getPageDict(title, lang):
    '''
    get the wikitext for a pagetitile for a lang
    '''
    params = {
        "action": "query",
        "prop": "revisions",
        "rvprop": "content|ids",
        "rvslots": "main",
        "rvlimit": 1,
        "titles": title,
        "format": "json",
        "formatversion": "2",
    }
    API_URL = "https://{0}.wikipedia.org/w/api.php".format(lang)
    headers = {"User-Agent": "mwaddlink"}
    req = requests.get(API_URL, headers=headers, params=params)
    res = req.json()
    res_page = res["query"]["pages"][0]
    res_rev = res_page["revisions"][0]

    wikitext = res_rev["slots"]["main"]["content"]
    revid = res_rev['revid']
    pageid = res_page["pageid"]
    result = {
        'pagetitle':title,
        'lang':lang,
        'wikitext':wikitext,
        'pageid':pageid,
        'revid':revid,
    }
    return result

##########################
## getting feature-dataset
##########################
from scipy.stats import kurtosis
from Levenshtein import distance as levenshtein_distance
from Levenshtein import jaro as levenshtein_score

def getDistEmb(ent_a, ent_b, embd):
    dst = 0
    try:## try if entities are in embd
        a = embd[ent_a]
        b = embd[ent_b]
        norm_ab = np.linalg.norm(b)*np.linalg.norm(a)
        ## if norm of any vector is 0, we assign dst=0 (maximum dst)
        if norm_ab==0:
            dst = 0.
        else:
            dst = np.dot(a,b)/norm_ab
#         dst = (np.dot(a, b) / np.linalg.norm(a) / np.linalg.norm(b))
    except:
        pass
    if np.isnan(dst):
        dst=0
    return dst

# Return the features for each link candidate in the context of the text and the page
def get_feature_set(page, text, link, anchors, word2vec, nav2vec):
    ngram = len(text.split()) # simple space based tokenizer to compute n-grams
    freq = anchors[text][link] # How many times was the link use with this text 
    ambig = len(anchors[text]) # home many different links where used with this text
    kur = kurtosis(sorted(list(anchors[text].values()), reverse = True) + [1] * (1000 - ambig)) # Skew of usage text/link distribution
    w2v = getDistEmb(page, link,word2vec) # W2V Distance between the source and target page
    nav = getDistEmb(page, link, nav2vec) # Nav Distance between the source and target page
    leven = levenshtein_score(text.lower(),link.lower())
    return (ngram, freq, ambig, kur, w2v, nav, leven)


##########################
## evaluation classification
##########################
# Main decision function.
# for a given page X and a piece of text "lipsum".. check all the candidate and make inference
# Returns the most likely candidate according to the pre-trained link model
# If the probability is below a certain threshold, return None
def classify_links(page, text, anchors, word2vec, nav2vec, model, threshold=0.95):
    #start_time = time.time()
    cand_prediction = {}
    # Work with the 10 most frequent candidates
    limited_cands = anchors[text]
    if len(limited_cands) > 10:
        limited_cands = dict(sorted(anchors[text].items(), key = operator.itemgetter(1), reverse = True)[:10]) 
    for cand in limited_cands:
        # get the features
#         cand_feats = get_feature_set(page, text, cand, anchors, word2vec,nav2vec,pageids)
        cand_feats = get_feature_set(page, text, cand, anchors, word2vec,nav2vec)

        # compute the model probability
        cand_prediction[cand] = model.predict_proba(np.array(cand_feats).reshape((1,-1)))[0,1]
    
    # Compute the top candidate
    top_candidate = max(cand_prediction.items(), key=operator.itemgetter(1))
    
    # Check if the max probability meets the threshold before returning
    if top_candidate[1] < threshold:
        return None
    #print("--- %s seconds ---" % (time.time() - start_time))
    return top_candidate

# Actual Linking function
def process_page(wikitext, page, anchors, pageids, redirects, word2vec,nav2vec, model, threshold = 0.8, pr = True, return_wikitext=True, context = 10):
    '''
    returns updated wikitext
    '''
    ## parse the wikicode
    page_wikicode = mwparserfromhell.parse(wikitext)
    
    page_wikicode_init= str(page_wikicode) # save the initial state
    
    ## get all existing links
    dict_links = getLinks(page_wikicode, redirects=redirects,pageids=pageids) ## get all links, resolve redirects
    linked_mentions = set(dict_links.keys())
    linked_links = set(dict_links.values())
    # inlcude also current pagetitle
    linked_mentions.add(normalise_anchor(page))
    linked_links.add(normalise_title(page))
    
    tested_mentions = set()
    added_links = []


    for node in page_wikicode.filter(recursive= False):
        # Parsing the tree can be done once 
        ## this is the most out loop to make sure the offset-calculations matches throughout
        if isinstance(node, Text):
            ## check the offset of the node in the wikitext_init
            node_val = node.value
            i1_node_init = page_wikicode_init.find(node_val)
            i2_node_init = i1_node_init + len(node_val)
            lines = node.split("\n")
            for line in lines:
                for sent in tokenizeSent(line):
                    for gram_length in range(10, 0, -1):
                        grams = list(ngrams(sent.split(), gram_length))                
                        for gram in grams:
                            mention = ' '.join(gram).lower()
                            mention_original = ' '.join(gram)
                            # if the mention exist in the DB 
                            # it was not previously linked (or part of a link)
                            # none of its candidate links is already used
                            # it was not tested before (for efficiency)
                            if (mention in anchors and
                                not any(mention in s for s in linked_mentions) and
                                not bool(set(anchors[mention].keys()) & linked_links) and
                                mention not in tested_mentions):
                                #logic
                                #print("testing:", mention, len(anchors[mention]))
                                candidate = classify_links(page, mention, anchors,word2vec,nav2vec, model, threshold=threshold)
                                if candidate:
                                    candidate_link, candidate_proba = candidate
                                    #print(">> ", mention, candidate)
                                    ############## Critical ##############
                                    # Insert The Link in the current wikitext
                                    match = re.compile(r'(?<!\[\[)(?<!-->)\b{}\b(?![\w\s]*[\]\]])'.format(re.escape(mention_original)))
                                    new_str = "[[" + candidate_link  +  "|" + mention_original
                                    ## add the probability
                                    if pr == True:
                                        new_str+="|pr=" + str(candidate_proba) 
                                    new_str += "]]"
                                    newval, found = match.subn(new_str, node.value, 1)
                                    node.value = newval
                                    ######################################
                                    # Book-keeping
                                    linked_mentions.add(mention)
                                    linked_links.add(candidate_link)
                                    if found==1:
                                        page_wikicode_init_substr = page_wikicode_init[i1_node_init:i2_node_init]
                                        i1_sub = page_wikicode_init_substr.lower().find(mention)
                                        startOffset = i1_node_init+i1_sub
                                        endOffset = startOffset+len(mention)
                                        ## provide context of the mention (+/- c characters in substring and wikitext)
                                        if context == None:
                                            context_wikitext = mention_original
                                            context_substring = mention_original
                                        else:
                                            ## context substring
                                            str_context = page_wikicode_init_substr
                                            i1_c = max([0,i1_sub-context])
                                            i2_c = min([len(str_context),i1_sub+len(mention_original)+context])
                                            context_substring = str_context[i1_c:i2_c]
                                            ## wikitext substring
                                            str_context = wikitext
                                            i1_c = max([0,startOffset-context])
                                            i2_c = min([len(str_context),endOffset+context])
                                            context_wikitext = str_context[i1_c:i2_c]
                                        new_link = {
                                            'linkTarget':candidate_link,
                                            'anchor':mention_original,
                                            'probability':float(candidate_proba),
                                            'startOffset':startOffset,
                                            'endOffset':endOffset,
                                            'context_wikitext':context_wikitext,
                                            'context_plaintext':context_substring
                                        }
                                        added_links += [new_link]
                                # More Book-keeping
                                tested_mentions.add(mention)
    ## if yes, we return the adapted wikitext
    ## else just return list of links with offsets
    if return_wikitext == True:
        return page_wikicode
    else:
        return added_links