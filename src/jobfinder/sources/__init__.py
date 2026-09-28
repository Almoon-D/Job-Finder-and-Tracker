"""Job sources. Importing this package registers every adapter."""

from . import email_alerts  # noqa: F401
from .ats import brassring, eightfold, oracle_hcm, simple, successfactors, workday  # noqa: F401
from .boards import adzuna, efinancialcareers, infojobs, jobcloud, linkedin  # noqa: F401
from .generic import html_list, json_api, jsonld, page_monitor, rss  # noqa: F401
