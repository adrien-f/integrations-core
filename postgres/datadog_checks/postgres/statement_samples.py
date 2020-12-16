import json
import time

import psycopg2
from cachetools import TTLCache
from datadog_checks.base.log import get_check_logger

try:
    import datadog_agent
except ImportError:
    from ..stubs import datadog_agent

from datadog import statsd
from concurrent.futures.thread import ThreadPoolExecutor

from datadog_checks.base.utils.db.sql import submit_statement_sample_events, compute_exec_plan_signature, \
    compute_sql_signature
from datadog_checks.base.utils.db.utils import ConstantRateLimiter

VALID_EXPLAIN_STATEMENTS = frozenset({'select', 'table', 'delete', 'insert', 'replace', 'update'})

# columns from pg_stat_activity which correspond to attributes common to all databases and are therefore stored in
# under other standard keys
pg_stat_activity_sample_exclude_keys = {
    # we process & obfuscate this separately
    'query',
    # stored separately
    'application_name',
    'datname',
    'usename',
    'client_addr',
    'client_hostname',
    'client_port',
}

class PostgresStatementSamples(object):
    # TODO: see what we should set max_workers to
    executor = ThreadPoolExecutor()

    """Collects telemetry for SQL statements"""

    def __init__(self, postgres_check):
        self.postgres_check = postgres_check
        self.config = postgres_check.config
        self.log = get_check_logger()
        self._rate_limiter = ConstantRateLimiter(self.config.collect_statement_samples_rate_limit)
        self._activity_last_query_start = None
        self._last_check_run = None
        self._stop_after_inactivity_seconds = 60
        self._future = None
        self._tags = None
        self._tags_str = None
        self._service = "postgres"
        # avoid reprocessing the exact same statements & plans
        self.seen_statements_cache = TTLCache(maxsize=1000, ttl=60)
        self.seen_statements_plan_sigs_cache = TTLCache(maxsize=1000, ttl=60)

    def run_sampler(self, tags):
        """
        start the sampler thread if not already running
        :param tags:
        :return:
        """
        self._tags = tags
        self._tags_str = ','.join(self._tags)
        for t in self._tags:
            if t.startswith('service:'):
                self._service = t[len('service:'):]
        self._last_check_run = time.time()
        if self._future is None or not self._future.running():
            self.log.info("starting postgres statement sampler")
            self._future = PostgresStatementSamples.executor.submit(self.collection_loop)
        else:
            self.log.debug("postgres statement sampler already running")

    def _get_new_pg_stat_activity(self, db, instance_tags=None):
        start_time = time.time()
        query = """
        SELECT * FROM {pg_stat_activity_view}
        WHERE datname = %s
        AND coalesce(TRIM(query), '') != ''
        """.format(
            pg_stat_activity_view=self.config.pg_stat_activity_view
        )
        db.rollback()
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            if self._activity_last_query_start:
                cursor.execute(query + " AND query_start > %s", (self.config.dbname, self._activity_last_query_start,))
            else:
                cursor.execute(query, (self.config.dbname,))
            rows = cursor.fetchall()

        statsd.histogram(
            "dd.postgres.get_new_pg_stat_activity.time", (time.time() - start_time) * 1000, tags=instance_tags
        )
        # TODO: once stable, either remove these development metrics or make them configurable in a debug mode
        statsd.histogram("dd.postgres.get_new_pg_stat_activity.rows", len(rows), tags=instance_tags)
        statsd.increment("dd.postgres.get_new_pg_stat_activity.total_rows", len(rows), tags=instance_tags)

        for r in rows:
            if r['query'] and r['datname'] and self.can_explain_statement(r['query']):
                if self._activity_last_query_start is None or r['query_start'] > self._activity_last_query_start:
                    self._activity_last_query_start = r['query_start']
                yield r

    def collection_loop(self):
        try:
            while True:
                self.collect_statement_samples()
                self._rate_limiter.sleep()
                if time.time() - self._last_check_run > self._stop_after_inactivity_seconds:
                    self.log.info("sampler collection_loop stopping due to check inactivity")
                    break
        except Exception:
            self.log.exception("statement sample collection loop failure")

    def collect_statement_samples(self):
        start_time = time.time()

        samples = self._get_new_pg_stat_activity(self.postgres_check.db, instance_tags=self._tags)
        events = self._explain_new_pg_stat_activity(self.postgres_check.db, samples, self._tags)
        submit_statement_sample_events(events)

        elapsed_ms = (time.time() - start_time) * 1000
        statsd.histogram(
            "dd.postgres.collect_statement_samples.time", elapsed_ms, tags=self._tags
        )
        statsd.gauge("dd.postgres.collect_statement_samples.seen_statements_cache.len", len(self.seen_statements_cache),
                     tags=self._tags)
        statsd.gauge(
            "dd.postgres.collect_statement_samples.seen_statement_plan_sigs_cache.len",
            len(self.seen_statements_plan_sigs_cache),
            tags=self._tags,
        )

    def can_explain_statement(self, statement):
        # TODO: cleaner query cleaning to strip comments, etc.
        if statement == '<insufficient privilege>':
            self.log.warn("Insufficient privilege to collect statement.")
            return False
        if statement.strip().split(' ', 1)[0].lower() not in VALID_EXPLAIN_STATEMENTS:
            return False
        if statement.startswith('SELECT {}'.format(self.config.collect_statement_samples_explain_function)):
            return False
        return True

    def _run_explain(self, db, statement, instance_tags=None):
        if not self.can_explain_statement(statement):
            return
        with db.cursor() as cursor:
            try:
                start_time = time.time()
                cursor.execute(
                    """SELECT {explain_function}($stmt${statement}$stmt$)""".format(
                        explain_function=self.config.collect_statement_samples_explain_function, statement=statement
                    )
                )
                result = cursor.fetchone()
                statsd.histogram("dd.postgres.run_explain.time", (time.time() - start_time) * 1000, tags=instance_tags)
            except psycopg2.errors.UndefinedFunction:
                self.log.warn(
                    "Failed to collect execution plan due to undefined explain_function: %s.",
                    self.config.collect_statement_samples_explain_function,
                )
                return None
            except Exception as e:
                statsd.increment("dd.postgres.run_explain.error", tags=instance_tags)
                self.log.error("failed to collect execution plan for query='%s'. (%s): %s", statement, type(e), e)
                return None
        if not result or len(result) < 1 or len(result[0]) < 1:
            return None
        return result[0][0]

    def _explain_new_pg_stat_activity(self, db, samples, instance_tags):
        for row in samples:
            original_statement = row['query']
            # TODO: should we cache for the obfuscated statement to avoid re-explaining the same normalized query?
            if original_statement in self.seen_statements_cache:
                continue
            self.seen_statements_cache[original_statement] = True
            plan_dict = self._run_explain(db, original_statement, instance_tags)

            # Plans have several important signatures to tag events with. Note that for postgres, the
            # query_signature and resource_hash will be the same value.
            # - `plan_signature` - hash computed from the normalized JSON plan to group identical plan trees
            # - `resource_hash` - hash computed off the raw sql text to match apm resources
            # - `query_signature` - hash computed from the raw sql text to match query metrics
            plan, normalized_plan, obfuscated_plan, plan_signature, plan_cost = None, None, None, None, None
            if plan_dict:
                plan = json.dumps(plan_dict)
                normalized_plan = datadog_agent.obfuscate_sql_exec_plan(plan, normalize=True)
                obfuscated_plan = datadog_agent.obfuscate_sql_exec_plan(plan)
                plan_signature = compute_exec_plan_signature(normalized_plan)
                plan_cost = (plan_dict.get('Plan', {}).get('Total Cost', 0.0) or 0.0)

            obfuscated_statement = datadog_agent.obfuscate_sql(original_statement)
            query_signature = compute_sql_signature(obfuscated_statement)
            apm_resource_hash = query_signature
            statement_plan_sig = (query_signature, plan_signature)
            if statement_plan_sig not in self.seen_statements_plan_sigs_cache:
                self.seen_statements_plan_sigs_cache[statement_plan_sig] = True
                event = {
                    # the timestamp for activity events is the time at which they were collected
                    "timestamp": time.time() * 1000,
                    # TODO: if "localhost" then use agent hostname instead
                    "host": self.config.host,
                    "service": self._service,
                    "ddsource": "postgres",
                    "ddtags": self._tags_str,
                    # no duration with postgres because these are in-progress, not complete events
                    # "duration": ?,
                    "network": {
                        "client": {
                            "ip": row.get('client_addr', None),
                            "port": row.get('client_port', None),
                            "hostname": row.get('client_hostname', None)
                        }
                    },
                    "db": {
                        "instance": row.get('datname', None),
                        "plan": {
                            "definition": obfuscated_plan,
                            "cost": plan_cost,
                            "signature": plan_signature
                        },
                        "query_signature": query_signature,
                        "resource_hash": apm_resource_hash,
                        "application": row.get('application_name', None),
                        "user": row['usename'],
                        "statement": obfuscated_statement
                    },
                    'postgres': {k: v for k, v in row.items() if k not in pg_stat_activity_sample_exclude_keys},
                }
                if self.config.collect_statement_samples_debug:
                    event['db']['debug'] = {
                        'original_plan': plan,
                        'normalized_plan': normalized_plan,
                        'original_statement': original_statement,
                    }
                yield event
