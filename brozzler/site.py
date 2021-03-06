# vim: set sw=4 et:

import surt
import json
import logging
import brozzler
import hashlib
import time

class Site(brozzler.BaseDictable):
    logger = logging.getLogger(__module__ + "." + __qualname__)

    def __init__(self, seed, id=None, job_id=None, scope=None, proxy=None,
        ignore_robots=False, time_limit=None, extra_headers=None,
        enable_warcprox_features=False, reached_limit=None, status="ACTIVE",
        claimed=False, start_time=time.time(), last_disclaimed=0,
        last_claimed_by=None):

        self.seed = seed
        self.id = id
        self.job_id = job_id
        self.proxy = proxy
        self.ignore_robots = ignore_robots
        self.enable_warcprox_features = bool(enable_warcprox_features)
        self.extra_headers = extra_headers
        self.time_limit = time_limit
        self.reached_limit = reached_limit
        self.status = status
        self.claimed = bool(claimed)
        self.last_claimed_by = last_claimed_by
        # times as seconds since epoch
        self.start_time = start_time
        self.last_disclaimed = last_disclaimed

        self.scope = scope or {}
        if not "surt" in self.scope:
            self.scope["surt"] = self._to_surt(seed)

    def __repr__(self):
        return """Site(id={},seed={},scope={},proxy={},enable_warcprox_features={},ignore_robots={},extra_headers={},reached_limit={})""".format(
                self.id, repr(self.seed), repr(self.scope),
                repr(self.proxy), self.enable_warcprox_features,
                self.ignore_robots, self.extra_headers, self.reached_limit)

    def _to_surt(self, url):
        hurl = surt.handyurl.parse(url)
        surt.GoogleURLCanonicalizer.canonicalize(hurl)
        hurl.query = None
        hurl.hash = None
        # XXX chop off path after last slash??
        return hurl.getURLString(surt=True, trailing_comma=True)

    def note_seed_redirect(self, url):
        new_scope_surt = self._to_surt(url)
        if not new_scope_surt.startswith(self.scope["surt"]):
            self.logger.info("changing site scope surt from {} to {}".format(self.scope["surt"], new_scope_surt))
            self.scope["surt"] = new_scope_surt

    def is_in_scope(self, url, parent_page=None):
        if parent_page and "max_hops" in self.scope and parent_page.hops_from_seed >= self.scope["max_hops"]:
            return False

        try:
            hurl = surt.handyurl.parse(url)

            # XXX doesn't belong here probably (where? worker ignores unknown schemes?)
            if hurl.scheme != "http" and hurl.scheme != "https":
                return False

            surtt = surt.GoogleURLCanonicalizer.canonicalize(hurl).getURLString(surt=True, trailing_comma=True)
            return surtt.startswith(self.scope["surt"])
        except:
            self.logger.warn("problem parsing url %s", repr(url))
            return False

class Page(brozzler.BaseDictable):
    def __init__(self, url, id=None, site_id=None, job_id=None,
            hops_from_seed=0, redirect_url=None, priority=None, claimed=False,
            brozzle_count=0, via_page_id=None, last_claimed_by=None):
        self.site_id = site_id
        self.job_id = job_id
        self.url = url
        self.hops_from_seed = hops_from_seed
        self.redirect_url = redirect_url
        self.claimed = bool(claimed)
        self.last_claimed_by = last_claimed_by
        self.brozzle_count = brozzle_count
        self.via_page_id = via_page_id
        self._canon_hurl = None

        if priority is not None:
            self.priority = priority
        else:
            self.priority = self._calc_priority()

        if id is not None:
            self.id = id
        else:
            digest_this = "site_id:{},canon_url:{}".format(self.site_id, self.canon_url())
            self.id = hashlib.sha1(digest_this.encode("utf-8")).hexdigest()

    def __repr__(self):
        return """Page(url={},job_id={},site_id={},hops_from_seed={})""".format(
                repr(self.url), self.job_id, self.site_id, self.hops_from_seed)

    def note_redirect(self, url):
        self.redirect_url = url

    def _calc_priority(self):
        priority = 0
        priority += max(0, 10 - self.hops_from_seed)
        priority += max(0, 6 - self.canon_url().count("/"))
        return priority

    def canon_url(self):
        if self._canon_hurl is None:
            self._canon_hurl = surt.handyurl.parse(self.url)
            surt.GoogleURLCanonicalizer.canonicalize(self._canon_hurl)
        return self._canon_hurl.geturl()

