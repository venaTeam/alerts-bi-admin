"""Automatic weekly reviews (design section 7.11).

* :mod:`~alerts_bi_admin.engine.weekly.plan` - which Monday-to-Monday UTC weeks are due for a team, as a pure
  function of its latest published week and the current time
* :mod:`~alerts_bi_admin.engine.weekly.runner` - running and publishing those weeks, one team at a time, under a
  database lock so two schedulers can never overlap

Each week is an ordinary single-team run: the same ``execute_run`` and ``persist_run`` the
command line calls, then the same ``publish_run`` an operator calls. Nothing here is a
second pipeline.
"""
