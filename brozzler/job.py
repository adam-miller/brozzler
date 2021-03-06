import logging
import brozzler
import yaml
import json
import datetime

def merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        merged = dict(a)
        b_tmp = dict(b)
        for k in a:
            merged[k] = merge(a[k], b_tmp.pop(k, None))
        merged.update(b_tmp)
        return merged
    elif isinstance(a, list) and isinstance(b, list):
        return a + b
    else:
        return a

def new_job_file(frontier, job_conf_file):
    logging.info("loading %s", job_conf_file)
    with open(job_conf_file) as f:
        job_conf = yaml.load(f)
        new_job(frontier, job_conf)

def new_job(frontier, job_conf):
    job = Job(id=job_conf.get("id"), conf=job_conf, status="ACTIVE", started=datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))
    frontier.new_job(job)

    sites = []
    for seed_conf in job_conf["seeds"]:
        merged_conf = merge(seed_conf, job_conf)
        # XXX check for unknown settings, invalid url, etc
    
        extra_headers = None
        if "warcprox_meta" in merged_conf:
            warcprox_meta = json.dumps(merged_conf["warcprox_meta"], separators=(',', ':'))
            extra_headers = {"Warcprox-Meta":warcprox_meta}
    
        site = brozzler.Site(job_id=job.id,
                seed=merged_conf["url"],
                scope=merged_conf.get("scope"),
                time_limit=merged_conf.get("time_limit"),
                proxy=merged_conf.get("proxy"),
                ignore_robots=merged_conf.get("ignore_robots"),
                enable_warcprox_features=merged_conf.get("enable_warcprox_features"),
                extra_headers=extra_headers)
        sites.append(site)

    for site in sites:
        new_site(frontier, site)

def new_site(frontier, site):
    logging.info("new site {}".format(site))
    frontier.new_site(site)
    try:
        if brozzler.is_permitted_by_robots(site, site.seed):
            page = brozzler.Page(site.seed, site_id=site.id, job_id=site.job_id, hops_from_seed=0, priority=1000)
            frontier.new_page(page)
            logging.info("queued page %s", page)
        else:
            logging.warn("seed url {} is blocked by robots.txt".format(site.seed))
    except brozzler.ReachedLimit as e:
        frontier.reached_limit(site, e)

class Job(brozzler.BaseDictable):
    logger = logging.getLogger(__module__ + "." + __qualname__)

    def __init__(self, id=None, conf=None, status="ACTIVE", started=None, finished=None):
        self.id = id
        self.conf = conf
        self.status = status
        self.started = started
        self.finished = finished

