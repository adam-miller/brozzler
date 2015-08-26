# vim: set sw=4 et:

__all__ = ["UnexpectedDbResult", "RethinkDbFrontier"]

import logging
import brozzler
import rethinkdb
r = rethinkdb
import random
import time

class UnexpectedDbResult(Exception):
    pass

class RethinkDbFrontier:
    logger = logging.getLogger(__module__ + "." + __qualname__)

    def __init__(self, servers=["localhost"], db="brozzler", shards=3, replicas=3):
        self.servers = servers
        self.db = db
        self.shards = shards
        self.replicas = replicas
        self._ensure_db()

    # https://github.com/rethinkdb/rethinkdb-example-webpy-blog/blob/master/model.py
    # "Best practices: Managing connections: a connection per request"
    def _random_server_connection(self):
        server = random.choice(self.servers)
        try:
            host, port = server.split(":")
            return r.connect(host=host, port=port)
        except ValueError:
            return r.connect(host=server)

    def _ensure_db(self):
        with self._random_server_connection() as conn:
            dbs = r.db_list().run(conn)
            if not self.db in dbs:
                self.logger.info("creating rethinkdb database %s", repr(self.db))
                r.db_create(self.db).run(conn)
            tables = r.db(self.db).table_list().run(conn)
            if not "sites" in tables:
                self.logger.info("creating rethinkdb table 'sites' in database %s", repr(self.db))
                r.db(self.db).table_create("sites", shards=self.shards, replicas=self.replicas).run(conn)
                r.db(self.db).table("sites").index_create("sites_last_disclaimed", [r.row["status"], r.row["claimed"], r.row["last_disclaimed"]]).run(conn)
            if not "pages" in tables:
                self.logger.info("creating rethinkdb table 'pages' in database %s", repr(self.db))
                r.db(self.db).table_create("pages", shards=self.shards, replicas=self.replicas).run(conn)
                r.db(self.db).table("pages").index_create("priority_by_site", [r.row["site_id"], r.row["brozzle_count"], r.row["claimed"], r.row["priority"]]).run(conn)
            # if not "jobs" in tables:
            #    r.db("test").table_create("jobs", shards=self.shards, replicas=self.replicas).run(conn)

    def _vet_result(self, result, **kwargs):
        self.logger.debug("vetting expected=%s result=%s", kwargs, result)
        # {'replaced': 0, 'errors': 0, 'skipped': 0, 'inserted': 1, 'deleted': 0, 'generated_keys': ['292859c1-4926-4b27-9d87-b2c367667058'], 'unchanged': 0}
        for k in ["replaced", "errors", "skipped", "inserted", "deleted", "unchanged"]:
            if k in kwargs:
                expected = kwargs[k]
            else:
                expected = 0
            if isinstance(expected, list):
                if result.get(k) not in kwargs[k]:
                    raise UnexpectedDbResult("expected {} to be one of {} in {}".format(repr(k), expected, result))
            else:
                if result.get(k) != expected:
                    raise UnexpectedDbResult("expected {} to be {} in {}".format(repr(k), expected, result))

    def new_site(self, site):
        self.logger.info("inserting into 'sites' table %s", site)
        with self._random_server_connection() as conn:
            result = r.db(self.db).table("sites").insert(site.to_dict()).run(conn)
            self._vet_result(result, inserted=1)
            site.id = result["generated_keys"][0]

    def update_site(self, site):
        self.logger.debug("updating 'sites' table entry %s", site)
        with self._random_server_connection() as conn:
            result = r.db(self.db).table("sites").get(site.id).replace(site.to_dict()).run(conn)
            self._vet_result(result, replaced=[0,1], unchanged=[0,1])

    def update_page(self, page):
        self.logger.debug("updating 'pages' table entry %s", page)
        with self._random_server_connection() as conn:
            result = r.db(self.db).table("pages").get(page.id).replace(page.to_dict()).run(conn)
            self._vet_result(result, replaced=[0,1], unchanged=[0,1])

    def new_page(self, page):
        self.logger.debug("inserting into 'pages' table %s", page)
        with self._random_server_connection() as conn:
            result = r.db(self.db).table("pages").insert(page.to_dict()).run(conn)
            self._vet_result(result, inserted=1)

    def claim_site(self):
        # XXX keep track of aggregate priority and prioritize sites accordingly?
        while True:
            with self._random_server_connection() as conn:
                result = (r.db(self.db).table("sites")
                        .between(["ACTIVE",False,0], ["ACTIVE",False,250000000000], index="sites_last_disclaimed")
                        .order_by(index="sites_last_disclaimed").limit(1).update({"claimed":True},return_changes=True).run(conn))
                self._vet_result(result, replaced=[0,1])
                if result["replaced"] == 1:
                    site = brozzler.Site(**result["changes"][0]["new_val"])
                else:
                    raise brozzler.NothingToClaim
            # XXX This is the only place we enforce time limit for now. Worker
            # loop should probably check time limit. Maybe frontier needs a
            # housekeeping thread to ensure that time limits get enforced in a
            # timely fashion.
            if not self._enforce_time_limit(site):
                return site

    def _enforce_time_limit(self, site):
        if (site.time_limit and site.time_limit > 0
                and time.time() - site.start_time > site.time_limit):
            self.logger.info("site FINISHED_TIME_LIMIT! time_limit=%s start_time=%s elapsed=%s %s",
                    site.time_limit, site.start_time, time.time() - site.start_time, site)
            site.status = "FINISHED_TIME_LIMIT"
            self.update_site(site)
            return True
        else:
            return False

    def claim_page(self, site):
        with self._random_server_connection() as conn:
            result = (r.db(self.db).table("pages")
                    .between([site.id,0,False,brozzler.MIN_PRIORITY], [site.id,0,False,brozzler.MAX_PRIORITY], index="priority_by_site")
                    .order_by(index=r.desc("priority_by_site")).limit(1)
                    .update({"claimed":True},return_changes=True).run(conn))
            self._vet_result(result, replaced=[0,1])
            if result["replaced"] == 1:
                return brozzler.Page(**result["changes"][0]["new_val"])
            else:
                raise brozzler.NothingToClaim

    def has_outstanding_pages(self, site):
        with self._random_server_connection() as conn:
            cursor = r.db(self.db).table("pages").between([site.id,0,False,brozzler.MIN_PRIORITY], [site.id,0,True,brozzler.MAX_PRIORITY], index="priority_by_site").limit(1).run(conn)
            return len(list(cursor)) > 0

    def get_page(self, page):
        with self._random_server_connection() as conn:
            result = r.db(self.db).table("pages").get(page.id).run(conn)
            if result:
                return brozzler.Page(**result)
            else:
                return None

    def completed_page(self, site, page):
        page.brozzle_count += 1
        page.claimed = False
        # XXX set priority?
        self.update_page(page)
        if page.redirect_url and page.hops_from_seed == 0:
            site.note_seed_redirect(page.redirect_url)
            self.update_site(site)

    def disclaim_site(self, site, page=None):
        self.logger.info("disclaiming %s", site)
        site.claimed = False
        site.last_disclaimed = time.time()
        if not page and not self.has_outstanding_pages(site):
            self.logger.info("site FINISHED! %s", site)
            site.status = "FINISHED"
        self.update_site(site)
        if page:
            page.claimed = False
            self.update_page(page)

    def scope_and_schedule_outlinks(self, site, parent_page, outlinks):
        counts = {"added":0,"updated":0,"rejected":0,"blocked":0}
        if outlinks:
            for url in outlinks:
                if site.is_in_scope(url, parent_page):
                    if brozzler.is_permitted_by_robots(site, url):
                        new_child_page = brozzler.Page(url, site_id=site.id, hops_from_seed=parent_page.hops_from_seed+1)
                        existing_child_page = self.get_page(new_child_page)
                        if existing_child_page:
                            existing_child_page.priority += new_child_page.priority
                            self.update_page(existing_child_page)
                            counts["updated"] += 1
                        else:
                            self.new_page(new_child_page)
                            counts["added"] += 1
                    else:
                        counts["blocked"] += 1
                else:
                    counts["rejected"] += 1

        self.logger.info("%s new links added, %s existing links updated, %s links rejected, %s links blocked by robots from %s",
            counts["added"], counts["updated"], counts["rejected"], counts["blocked"], parent_page)
