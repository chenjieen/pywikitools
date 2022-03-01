"""
This script produces a translated ODT file for a given worksheet and a given language.
It does so by:
1. accessing the worksheet in the mediawiki system together with its translation
2. downloading the English ODT file (the URL is found in the result of the first step)
3. doing search and replace: For each translation unit
   - do some cleansing (removing links, unnecessary spaces)
   - split it up even further into small snippets (when the translation unit contains lists etc.)
   - search for each snippet and replace it by its translation
4. saving the created ODT file

It does quite some logging:
    - error level: serious issues where the script had to be aborted
    - warning level: these should be checked afterwards
    - info level: going along what the scrip does
    - debug level: extensive details for debugging

Command line options:
    -h, --help: help message
    -l [debug, info, warning, error]: set loglevel
    --keep-english-file: don't delete the downloaded English ODT file after we're finished
"""
import sys
import logging
import shlex
import getopt
import os.path
import re
from subprocess import Popen, TimeoutExpired
from time import sleep
import configparser
from typing import List, Optional
import requests
import uno          # type: ignore
from com.sun.star.connection import NoConnectException
from com.sun.star.beans import PropertyValue
from com.sun.star.lang import Locale
#from com.sun.star.lang import IllegalArgumentException #could this be helpful to check oo arguments?
import fortraininglib
from lang.libreoffice_lang import LANG_LOCALE
from pywikitools.lang.translated_page import TranslationUnit

def usage():
    print("Usage: python3 translateodt.py [-l loglevel] [--keep-english-file] worksheetname languagecode")

PORT = 2002             # port where libreoffice is running
CONNECT_TRIES = 10      # how often we re-try to connect to libreoffice
TIMEOUT = 200           # The script will be aborted if it's running longer than this amount of seconds
SNIPPET_WARN_LENGTH = 4 # give a warning when search or replace string is shorter than 4 characters
# The following templates don't contain any translation units and can be ignored
IGNORE_TEMPLATES = ['Template:DocDownload', 'Template:OdtDownload', 'Template:PdfDownload',
                    'Template:Translatable template', 'Template:Version', 'Module:Template translation',
                    'Template:Italic']
# for the following languages we don't add ", version x.y" to the keywords in the document properties
# because the translation of "version" is very close to the English word "version"
# TODO should 'ko' be in this list?
NO_ADD_ENGLISH_VERSION = ['de', 'pt-br', 'cs', 'nl', 'fr', 'id', 'ro', 'es', 'sv', 'tr', 'tr-tanri']

keep_english_file = False   # Delete the English worksheet after we're done (can be changed with the --keep-english-file arg)
logger = logging.getLogger('pywikitools.translateodt')

# Read configuration from config.ini in this folder; set default values in case it doesn't exist
config = configparser.ConfigParser()
config.read_dict({'Paths' : {'worksheets' : os.path.abspath(os.getcwd()) + '/worksheets/'},
                  'translateodt' : {'closeoffice': True,
                                    'headless': False}})
config.read(os.path.dirname(os.path.abspath(__file__)) + '/config.ini')

############################################################################################
# Helping functions and parameters for communicating with oo
############################################################################################
def open_doc(name: str):
    """ Opens an existing libre office document
    Args:
        name: Filename
    TODO: raise error or meaningful return value when it didn't work
    """
    logger.info(f"Opening file {name}")

    # get the uno component context from the PyUNO runtime
    ctx = None
    model = None
    args = 'soffice ' + shlex.quote(name)
    if config.getboolean('translateodt', 'headless'):
        args += " --headless"
    args += ' --accept="socket,host=localhost,port=' + str(PORT) + ';urp;StarOffice.ServiceManager"'
    logger.debug(args)
    proc = Popen(args, shell=True)
    local_context = uno.getComponentContext()

    # create the UnoUrlResolver
    resolver = local_context.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local_context)

    # connect to the running office
    retries = 0
    ctx = None
    while ctx is None:
        try:
            ctx = resolver.resolve(f"uno:socket,host=localhost,port={PORT};urp;StarOffice.ComponentContext")
        except NoConnectException as error:
            retries += 1
            logger.debug(f"Failed to connect to office. This is attempt #{retries}")
            if retries > CONNECT_TRIES:
                logger.warning(f"Couldn't connect to LibreOffice. Tried {CONNECT_TRIES} times, giving up now.")
                raise error
            sleep(2)

    # get the central desktop object
    desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)

    # access the current writer document
    while not model:
        model = desktop.getCurrentComponent()
        sleep(1)

    search_ready = False
    retries = 0
    # make sure createSearchDescriptor is ready
    # otherwise sometimes running the script failed when the file wasn't completely loaded yet
    while not search_ready:
        try:
            model.createSearchDescriptor()
        except AttributeError as error:
            logger.error(f"AttributeError: {error}")
            retries += 1
            logger.debug(f"createSearchDescriptor() failed. This is attempt #{retries}")
            if retries > CONNECT_TRIES:
                logger.warning("Error trying to access the LibreOffice document."
                               f"createSearchDescriptor failed {CONNECT_TRIES} times, giving up now.")
                sys.exit(2)
            sleep(2)
        else:
            logger.debug('createSearchDescriptor() directly was successful.')
            search_ready = True

    sleep(2)    # sometimes the loading of the document isn't complete yet but the script already continues and doesn't find anything. Maybe this helps a bit
    return (desktop, model, proc)

def remove_links(text: str) -> str:
    """
    Remove links. Warns also if there is a link without |
    Example: [[Prayer]] causes a warning, correct would be [[Prayer|Prayer]].
    We have this convention so that translators are less confused as they need to write e.g. [[Prayer/de|Gebet]]
    @return the processed string
    """
    # This does all necessary replacements if the link correctly uses the form [[destination|description]]
    cleansed_text = re.sub(r"\[\[(.*?)\|(.*?)\]\]", r"\2", text)

    # Now we check for links who are not following the convention
    # We need to remove the # of an internal link, otherwise it gets the meaning of a numbering. (#?) does the trick
    pattern = re.compile(r"\[\[(#?)(.*?)\]\]")
    match = pattern.search(cleansed_text)
    if match:
        logger.warning(f"The following link is errorneous: {match.group(0)}. "
                       f"It needs to look like [[English destination/language code|{match.group(2)}]]. Please correct.")
        cleansed_text = pattern.sub(r"\2", cleansed_text)

    return cleansed_text

def check_before_search_and_replace(orig: str, trans: str) -> bool:
    """
    Checks if we need to do a search and replace or if there are other exceptions
    Logs warnings for certain circumstances
    @return true if we need to do search and replace
    """
    # if string is empty there is nothing to do
    if orig == '':
        return False

    # if string is a file name, we ignore it
    if orig.endswith(('.pdf', '.odt', '.doc')):
        return False

    if orig == trans:
        logger.debug(f"Search and replace string are identical, ignoring: {orig}")
        return False

    if len(orig) < SNIPPET_WARN_LENGTH:
        if (orig in [' ', '.', ',', ':', ';']):
            logger.warning("Warning: Problematic search string detected! Please check and correct. "
                           f"Replaced {orig} with {trans}")
        else:
            logger.warning("Potential problem: short search string. This can be totally normal but please check. "
                           f"Replaced {orig} with {trans}")
    return True


def process_snippet(oo_data, orig: str, trans: str):
    """
    Looks at one snippet, does some preparations and tries to do search and replace
    @param oo_data TODO get rid of that
    @param orig the original string (what to search for)
    @param trans the translated string (what we're going to replace it with)
    """
    logger.debug(f"process_snippet, orig: {orig}, trans: {trans}")
    orig = orig.strip()
    trans = trans.strip()

    if not check_before_search_and_replace(orig, trans):
        return
    # if translation snippet can be found in document, replace
    try:
        replaced = search_and_replace(oo_data, orig, trans)
        if replaced:
            logger.info(f"Replaced: {orig} with: {trans}")
        else:
            # Second try: split at newlines (or similar strange breaks) and try again
            logger.info(f"Couldn't find {orig}. Splitting at newlines and trying again.")

            orig_split = re.split("[\t\n\r\f\v]", orig)
            trans_split = re.split("[\t\n\r\f\v]", trans)
            if len(orig_split) != len(trans_split):
                logger.warning("Couldn't process the following translation snippet. Please check.")
                logger.warning(f"Original: \n{orig}")
                logger.warning(f"Translation: \n{trans}")
                return
            for _, (search, replace) in enumerate(zip(orig_split, trans_split)):
                if not check_before_search_and_replace(search.strip(), replace.strip()):
                    continue
                replaced = search_and_replace(oo_data, search, replace)
                if replaced:
                    logger.info(f"Replaced: {search} with: {replace}")
                else:
                    logger.warning(f"Not found:\n{search}\nTranslation:\n{replace}")

    except AttributeError as error:
        logger.error(f"AttributeError: {error}")  # todo: wait some seconds and try again
    return


def search_and_replace(oo_data, string_orig, string_rep):
    """ replaces FIRST string like string_orig in a libre office document
    Args:
        oo_data: data of office document
        string_orig: string that will be replaced
        string_rep: string that is inserted instead
    """
    ## source: https://wiki.openoffice.org/wiki/Documentation/BASIC_Guide/Editing_Text_Documents
    desktop, model, proc = oo_data
    search = model.createSearchDescriptor()
    search.SearchCaseSensitive = True
    search.SearchString = string_orig

    found = bool(model.findFirst(search))
    if found:
        found_x = model.findFirst(search)
        found_x.setString(string_rep)
    return found

def oo_save_close(oo_data, filename):
    """ Saves and closes office
    @param oo_data [desktop, model, proc]
    @param filename where to save the odt file (full URL, e.g. /home/user/worksheets/de/Gebet.odt )
    """
    desktop, model, proc = oo_data

    uri = 'file://' + filename
    args = []   # arguments for saving

    # overwrite file if it already exists
    arg0 = PropertyValue()
    arg0.Name = "Overwrite"
    arg0.Value = True
    args.append(arg0)

    model.storeAsURL(uri, args) # save as ODT
    logger.info(f"Saved translated document with uri {uri}")

    opts = []   # options for PDF export
    # Archive PDF/A
    opt1 = PropertyValue()
    opt1.Name = "SelectPdfVersion"
    opt1.Value = 1
    opts.append(opt1)
    # reduce image resolution to 300dpi
    opt2 = PropertyValue()
    opt2.Name = "MaxImageResolution"
    opt2.Value = 300
    opts.append(opt2)
    # Export bookmarks
    opt3 = PropertyValue()
    opt3.Name = "ExportBookmarks"
    opt3.Value = True
    opts.append(opt3)
    # 90% JPEG image compression
    opt4 = PropertyValue()
    opt4.Name = "Quality"
    opt4.Value = 90
    opts.append(opt4)

    # export to pdf property
    arg1 = PropertyValue()
    arg1.Name = "FilterName"
    arg1.Value = "writer_pdf_Export"
    args.append(arg1)

    # collect options
    arg2 = PropertyValue()
    arg2.Name = "FilterData"
    arg2.Value = uno.Any("[]com.sun.star.beans.PropertyValue", tuple(opts))
    args.append(arg2)

    # export as pdf
    model.storeToURL(uri.replace(".odt", ".pdf"), tuple(args))
    logger.info(f"Exported translated document to PDF with uri {uri.replace('.odt', '.pdf')}")

    # close
    if config.getboolean('translateodt', 'closeoffice'):
        desktop.terminate()
    try:
        return proc.wait(timeout=TIMEOUT)
    except TimeoutExpired:
        logger.error(f"soffice process didn't terminate within {TIMEOUT}s. Killing it.")
        proc.kill()
        return 2

def split_translation_unit(text: str, fallback: bool = False) -> List[str]:
    """
    Takes a translation unit and splits it into snippets than can be searched and replaced
    @param text: The translation unit we want to split
    @param fallback: Should we try the fallback splitting-up?
    @return list of strings
    """
    # Split at all kinds of formattings:
    # '' or ''': italic / bold formatting
    # <tags>: all kind of html tags like <i> or <b> or </i> or </b>
    # * or #: bullet list / numbered list items
    # == up to ======: section headings
    # : at the beginning of a line: definition list / indent text
    # ; at the beginning of a line: definition list
    pattern = re.compile("\'\'+|<.*?>|[*#]|={2,6}|^:|^;", flags=re.MULTILINE)

    if fallback:
        # We replace <br/> line breaks by \n line breaks
        # and remove italic and bold formatting and all kind of <tags>
        text = re.sub("< *br */ *>", '\n', text)
        text = re.sub("\'\'+|<.*?>", '', text, flags=re.MULTILINE)

    return pattern.split(text)


def translateodt(worksheet: str, languagecode: str) -> Optional[str]:
    """ Central function to process the given worksheet
    @param worksheet name of the worksheet (e.g. "Forgiving_Step_by_Step")
    @param languagecode what language we should translate to (e.g. "de")
    @return file name of the created odt file (or None in case of error)
    """
    translations = fortraininglib.get_translation_units(worksheet, languagecode)
    if isinstance(translations, str):
        # This means we couldn't get the translation units so we can't do anything
        logger.error(translations)
        return None

    # Check for templates we need to read as well
    templates = set(fortraininglib.list_page_templates(worksheet)) - set(IGNORE_TEMPLATES)
    for template in templates:
        response = fortraininglib.get_translation_units(template, languagecode)
        if isinstance(response, str):
            logger.warning(f"Couldn't get translations of {template}, ignoring this template.")
        else:
            translations.extend(response)

    # find out version, name of original odt-file and name of translated odt-file
    version = None
    version_orig = None
    odt = None
    filename = None
    for t in translations:
        if re.search(r"\.odt", t["definition"]):
            odt = t["definition"]
            filename = t["translation"]
        # Searching for version number (valid examples: 1.0; 2.1; 0.7b; 1.5a)
        if re.search(r"^\d\.\d[a-zA-Z]?$", t["definition"]):
            if t["translation"] != t["definition"]:
                logger.warning(f"English original has version {t['definition']}, "
                               f"translation has version {t['translation']}. "
                               "Please update translation. "
                               "Ask an administrator for a list of changes in the English original.")
            version = t["translation"]
            version_orig = t["definition"]

    if not odt:
        logger.error(f"Couldn't find name of odt file in page {worksheet}")
        return None
    if not version_orig:
        logger.error(f"Couldn't find version number in page {worksheet}")
        return None
    if not version:
        logger.warning("Translation of version is missing!")
        version = version_orig
    if not filename:
        logger.warning("Translation of file name is missing!")

    # add footer (Template:CC0Notice) to translation list
    translations.extend([{
        "definition": fortraininglib.get_cc0_notice(version_orig, 'en'),
        "translation": fortraininglib.get_cc0_notice(version, languagecode)}])

    en_path = config['Paths']['worksheets'] + 'en'
    if not os.path.isdir(en_path):
        os.makedirs(en_path)
    odt_path = en_path + '/' + odt
    if os.path.isfile(odt_path):
        logger.warning(f"File {odt_path} already exists locally, not downloading.")
    else:
        url = fortraininglib.get_file_url(odt)
        if url is None:
            logger.error(f"Could not get URL of file {odt}")
            return None

        odt_doc = requests.get(url, allow_redirects=True)
        with open(odt_path, 'wb') as fh:
            fh.write(odt_doc.content)
        logger.info(f"Successfully downloaded and saved {odt_path}")

    #sleep(30)

    ############################################################################################
    # Open and Translate odt
    ############################################################################################

    # open document and replace through translations
    oo_data = open_doc(odt_path)

    # for each translation unit:
    for t in translations:
        orig = t["definition"]
        trans = t["translation"]

        if not isinstance(orig, str):
            logger.warning("Empty unit in original detected")
            continue
        if not isinstance(trans, str):
            logger.warning(f"Translation missing. Please translate the following part: {orig}")
            continue
        if orig == version_orig:
            # We don't try to do search and replace with the version string. We later process the whole CC0 notice
            continue

        logger.debug(f"Translation unit: {orig}")
        # Preprocessing: remove links
        orig = remove_links(orig)
        trans = remove_links(trans)

        # Check if number of <br/> is equal, otherwise replace by newline
        br_in_orig = len(re.split("< *br */ *>", orig)) - 1
        br_in_trans = len(re.split("< *br */ *>", trans)) - 1
        if br_in_orig != br_in_trans:
            # TODO in the future remove this? At least find out how much this is necessary
            logger.info("Number of <br/> differs between original and translations. Replacing with newlines.")
            orig = re.sub("< *br */ *>", '\n', orig)
            trans = re.sub("< *br */ *>", '\n', trans)

        identifier = t["key"] if "key" in t else ""
        translation_unit = TranslationUnit(identifier, languagecode, orig, trans)
        if not translation_unit.is_translation_well_structured():
            # We can't process this translation unit. Logging messages are already written
            continue
        if br_in_orig != br_in_trans:
            logger.warning(f"Issue with <br/> (line breaks). There are {br_in_orig} in the original "
                           f"but {br_in_trans} of them in the translation. "
                           f"We still can process {t['title']}. You may ignore this warning.")

        # for each snippet of translation unit:
        for (search, replace) in translation_unit:
            process_snippet(oo_data, search.content, replace.content)

    ############################################################################################
    # Set properties
    ############################################################################################
    desktop, model, proc = oo_data
    docProps = model.getDocumentProperties()

    # check if there is a subtitle in docProps.Subject:
    subtitle_en = ""
    subtitle_lan = ""
    if docProps.Subject != "":
        if docProps.Subject != translations[1]['definition']:
            logger.info(f"Assuming we have no subtitle. Subject in properties is {docProps.Subject}"
                        f", but second translation unit is {translations[1]['definition']}")
        else:
            subtitle_en = " - " + translations[1]['definition']
            subtitle_lan = " - " + translations[1]['translation']

    # Title: [translated Title]
    headline = translations[0]['translation']
    if headline is None:
        logger.error("Headline doesn't seem to be translated. Exiting now.")
        return None
    docProps.Title = headline
    docProps.Title += subtitle_lan


    # Subject: [English title] [Languagename in English] [Languagename autonym]
    docProps.Subject = str(translations[0]['definition'])
    docProps.Subject += subtitle_en
    docProps.Subject += " " + str(fortraininglib.get_language_name(languagecode, 'en'))
    docProps.Subject += " " + str(fortraininglib.get_language_name(languagecode))

    # Keywords: [Translated copyright notice with replaced version number] - copyright-free, version [versionnumber]
    # ",version [versionnumber]" is omitted in languages where the translation of "version" is very similar
    cc0_notice = fortraininglib.get_cc0_notice(version, languagecode) + " - copyright-free"
    if languagecode not in NO_ADD_ENGLISH_VERSION:
        if re.search(r"^[0-9]\.[0-9][a-zA-Z]?$", version):
            cc0_notice += ", version " + version
        else:
            cc0_notice += ", version " + version_orig
            logger.warning("Version number seems not to use standard decimal numbers."
                           f"Assuming this is identical to {version_orig}. Please check File->Properties->Keywords")
    docProps.Keywords = [cc0_notice]

    # create filename from headline
    filename_check = re.sub(" ", '_', headline)
    filename_check = re.sub("[':]", "", filename_check)
    filename_check += ".odt"
    if filename != filename_check:
        logger.warning("Warning: Is the file name not correctly translated? Please correct. "
                       f"Translation: {filename}, according to the headline it should be: {filename_check}")
        filename = filename_check

    par_styles = model.getStyleFamilies().getByName("ParagraphStyles")
    default_style = None
    if par_styles.hasByName('Default Style'):       # until LibreOffice 6
        default_style = par_styles.getByName('Default Style')
    elif par_styles.hasByName('Default Paragraph Style'):
        # got renamed in LibreOffice 7, see https://bugs.documentfoundation.org/show_bug.cgi?id=129568
        default_style = par_styles.getByName('Default Paragraph Style')
    else:
        logger.warning("Couldn't find Default Style in paragraph styles."
                       "Can't set RTL and language locale, please do that manually.")

    if default_style is not None:
        if fortraininglib.get_language_direction(languagecode) == "rtl":
            logger.debug("Setting language direction to RTL")
            default_style.ParaAdjust = 1 # alignment (0: left; 1: right; 2: justified; 3: center)
            default_style.WritingMode = 1 # writing direction (0: LTR; 1: RTL; 4: "use superordinate object settings")

        # default_style.CharLocale.Language and .Country seem to be read-only
        logger.debug("Setting language locale of Default Style")
        if languagecode in LANG_LOCALE:
            lang = LANG_LOCALE[languagecode]
            struct_locale = lang.to_locale()
            logger.info(f"Assigning Locale for language '{languagecode}': {lang}")
            if lang.is_standard():
                default_style.CharLocale = struct_locale
            if lang.is_asian():
                default_style.CharLocaleAsian = struct_locale
            if lang.is_complex():
                default_style.CharLocaleComplex = struct_locale
            if lang.has_custom_font():
                logger.warning(f'Using font "{lang.get_custom_font()}". Please make sure you have it installed.')
                default_style.CharFontName = lang.get_custom_font()
                default_style.CharFontNameAsian = lang.get_custom_font()
                default_style.CharFontNameComplex = lang.get_custom_font()
        else:
            logger.warning(f"Language '{languagecode}' not in LANG_LOCALE. Please ask an administrator to fix this.")
            struct_locale = Locale(languagecode, "", "")
            # We don't know which of the three this language belongs to... so we assign it to all Fontstyles
            # (unfortunately e.g. 'ar' can be assigned to "Western Font" so try-and-error-assigning doesn't work)
            default_style.CharLocale = struct_locale
            default_style.CharLocaleAsian = struct_locale
            default_style.CharLocaleComplex = struct_locale

    # save in folder worksheets/[languagecode]/ as odt and pdf, close open office
    save_path = config['Paths']['worksheets'] + languagecode
    if not os.path.isdir(save_path):
        os.makedirs(save_path)
    file_path = save_path + '/' + filename
    oo_save_close(oo_data, file_path)

    if keep_english_file:
        logger.info(f"Keeping {odt_path}")
    else:
        logger.debug(f"Removing {odt_path}")
        os.remove(odt_path)
    return file_path

# Check if the script is run as standalone or called by another script
if __name__ == '__main__':
    ############################################################################################
    # Check inputs
    ############################################################################################
    try:
        opts, args = getopt.getopt(sys.argv[1:], "hl:", ["help", "loglevel", "keep-english-file"])
    except getopt.GetoptError as err:
        # print help information and exit:
        print(err)
        usage()
        sys.exit(2)
    if (len(args) != 2):
        usage()
        sys.exit(2)
    worksheetname = args[0]
    languagecode = args[1]
    for o, a in opts:
        if o == "-l":
            numeric_level = getattr(logging, a.upper(), None)
            if not isinstance(numeric_level, int):
                raise ValueError(f"Invalid log level: {a}")
            logging.basicConfig(level=numeric_level)
            logger.setLevel(numeric_level)
        elif o in ("-h", "--help"):
            usage()
            sys.exit()
        elif o == "--keep-english-file":
            keep_english_file = True
        else:
            logger.warning(f"Unhandled option: {o}")
    logger.debug(f"Worksheetname: {worksheetname}, languagecode: {languagecode}")
    translateodt(worksheetname, languagecode)
