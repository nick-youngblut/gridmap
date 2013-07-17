# -*- coding: utf-8 -*-

# Written (W) 2008-2012 Christian Widmer
# Written (W) 2008-2010 Cheng Soon Ong
# Written (W) 2012-2013 Daniel Blanchard, dblanchard@ets.org
# Copyright (C) 2008-2012 Max-Planck-Society, 2012-2013 ETS

# This file is part of Grid Map.

# Grid Map is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Grid Map is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with Grid Map.  If not, see <http://www.gnu.org/licenses/>.

"""
This module provides wrappers that simplify submission and collection of jobs,
in a more 'pythonic' fashion.

@author: Christian Widmer
@author: Cheng Soon Ong
@author: Dan Blanchard (dblanchard@ets.org)
"""

from __future__ import absolute_import, print_function, unicode_literals

import inspect
import os
import subprocess
import sys
import traceback
import uuid
from socket import gethostname
from time import sleep

import drmaa
from redis import StrictRedis
from redis.exceptions import ConnectionError as RedisConnectionError

from gridmap.data import clean_path, zload_db, zsave_db

# Python 2.x backward compatibility
if sys.version_info < (3, 0):
    range = xrange


#### Global settings ####
# Redis settings
REDIS_DB = 2
REDIS_PORT = 7272

# Is mem_free configured properly on the cluster?
USE_MEM_FREE = False

# Which queue should we use by default
DEFAULT_QUEUE = 'all.q'


class Job(object):
    """
    Central entity that wraps a function and its data. Basically, a job consists
    of a function, its argument list, its keyword list and a field "ret" which
    is filled, when the execute method gets called.

    @note: This can only be used to wrap picklable functions (i.e., those that
    are defined at the module or class level).
    """

    __slots__ = ('_f', 'args', 'jobid', 'kwlist', 'cleanup', 'ret', 'exception',
                 'environment', 'replace_env', 'working_dir', 'num_slots',
                 'mem_free', 'white_list', 'path', 'uniq_id', 'name', 'queue')

    def __init__(self, f, args, kwlist=None, cleanup=True, mem_free="1G",
                 name='gridmap_job', num_slots=1, queue=DEFAULT_QUEUE):
        """
        Initializes a new Job.

        @param f: a function, which should be executed.
        @type f: function
        @param args: argument list of function f
        @type args: list
        @param kwlist: dictionary of keyword arguments for f
        @type kwlist: dict
        @param cleanup: flag that determines the cleanup of input and log file
        @type cleanup: boolean
        @param mem_free: Estimate of how much memory this job will need (for
                         scheduling)
        @type mem_free: C{basestring}
        @param name: Name to give this job
        @type name: C{basestring}
        @param num_slots: Number of slots this job should use.
        @type num_slots: C{int}
        @param queue: SGE queue to schedule job on.
        @type queue: C{basestring}
        """

        self.path = None
        self._f = None
        self.function = f
        self.args = args
        self.jobid = -1
        self.kwlist = kwlist if kwlist is not None else {}
        self.cleanup = cleanup
        self.ret = None
        self.environment = None
        self.replace_env = False
        self.working_dir = os.getcwd()
        self.num_slots = num_slots
        self.mem_free = mem_free
        self.white_list = []
        self.uniq_id = None
        self.name = name.replace(' ', '_')
        self.queue = queue

    @property
    def function(self):
        ''' Function this job will execute. '''
        return self._f

    @function.setter
    def function(self, f):
        """
        setter for function that carefully takes care of
        namespace, avoiding __main__ as a module
        """

        m = inspect.getmodule(f)
        try:
            self.path = clean_path(os.path.dirname(os.path.abspath(
                inspect.getsourcefile(f))))
        except TypeError:
            self.path = ''

        # if module is not __main__, all is good
        if m.__name__ != "__main__":
            self._f = f

        else:

            # determine real module name
            mn = os.path.splitext(os.path.basename(m.__file__))[0]

            # make sure module is present
            __import__(mn)

            # get module
            mod = sys.modules[mn]

            # set function from module
            self._f = getattr(mod, f.__name__)

    def execute(self):
        """
        Executes function f with given arguments
        and writes return value to field ret.
        If an exception is encountered during execution, ret will
        contain a pickled version of it.
        Input data is removed after execution to save space.
        """
        try:
            self.ret = self.function(*self.args, **self.kwlist)
        except Exception as exception:
            self.ret = exception
            traceback.print_exc()
        del self.args
        del self.kwlist

    @property
    def native_specification(self):
        """
        define python-style getter
        """

        ret = ""

        if self.name:
            ret += " -N {0}".format(self.name)
        if self.mem_free and USE_MEM_FREE:
            ret += " -l mem_free={0}".format(self.mem_free)
        if self.num_slots and self.num_slots > 1:
            ret += " -pe smp {0}".format(self.num_slots)
        if self.white_list:
            ret += " -l h={0}".format('|'.join(self.white_list))
        if self.queue:
            ret += " -q {0}".format(self.queue)

        return ret


def _submit_jobs(jobs, uniq_id, temp_dir='/scratch', white_list=None,
                 quiet=True):
    """
    Method used to send a list of jobs onto the cluster.
    @param jobs: list of jobs to be executed
    @type jobs: c{list} of L{Job}
    @param uniq_id: The unique suffix for the tables corresponding to this job
                    in the database.
    @type uniq_id: C{basestring}
    @param temp_dir: Local temporary directory for storing output for an
                     individual job.
    @type temp_dir: C{basestring}
    @param white_list: List of acceptable nodes to use for scheduling job. If
                       None, all are used.
    @type white_list: C{list} of C{basestring}
    @param quiet: When true, do not output information about the jobs that have
                  been submitted.
    @type quiet: C{bool}
    """

    session = drmaa.Session()
    session.initialize()
    jobids = []

    for job_num, job in enumerate(jobs):
        # set job white list
        job.white_list = white_list

        # append jobs
        jobid = _append_job_to_session(session, job, uniq_id, job_num,
                                       temp_dir=temp_dir, quiet=quiet)
        jobids.append(jobid)

    sid = session.contact
    session.exit()

    return (sid, jobids)


def _append_job_to_session(session, job, uniq_id, job_num, temp_dir='/scratch/',
                           quiet=True):
    """
    For an active session, append new job based on information stored in job
    object. Also sets job.job_id to the ID of the job on the grid.

    @param session: The current DRMAA session with the grid engine.
    @type session: C{drmaa.Session}
    @param job: The Job to add to the queue.
    @type job: L{Job}
    @param uniq_id: The unique suffix for the tables corresponding to this job
                    in the database.
    @type uniq_id: C{basestring}
    @param job_num: The row in the table to store/retrieve data on. This is only
                    non-zero for jobs created via grid_map.
    @type job_num: C{int}
    @param temp_dir: Local temporary directory for storing output for an
                    individual job.
    @type temp_dir: C{basestring}
    @param quiet: When true, do not output information about the jobs that have
                  been submitted.
    @type quiet: C{bool}
    """

    jt = session.createJobTemplate()

    # fetch env vars from shell
    shell_env = os.environ

    if job.environment and job.replace_env:
        # only consider defined env vars
        jt.jobEnvironment = job.environment

    elif job.environment and not job.replace_env:
        # replace env var from shell with defined env vars
        env = shell_env
        env.update(job.environment)
        jt.jobEnvironment = env

    else:
        # only consider env vars from shell
        jt.jobEnvironment = shell_env

    # Run module using python -m to avoid ImportErrors when unpickling jobs
    jt.remoteCommand =  sys.executable
    jt.args = ['-m', 'gridmap.runner', '{0}'.format(uniq_id),
               '{0}'.format(job_num), job.path, temp_dir, gethostname()]
    jt.nativeSpecification = job.native_specification
    jt.outputPath = ":" + temp_dir
    jt.errorPath = ":" + temp_dir

    jobid = session.runJob(jt)

    # set job fields that depend on the jobid assigned by grid engine
    job.jobid = jobid

    if not quiet:
        print('Your job {0} has been submitted with id {1}'.format(job.name,
                                                                   jobid),
              file=sys.stderr)

    session.deleteJobTemplate(jt)

    return jobid


def _collect_jobs(sid, jobids, joblist, redis_server, uniq_id,
                  temp_dir='/scratch/', wait=True):
    """
    Collect the results from the jobids, returns a list of Jobs

    @param sid: session identifier
    @type sid: string returned by cluster
    @param jobids: list of job identifiers returned by the cluster
    @type jobids: list of strings
    @param redis_server: Open connection to the database where the results will
                         be stored.
    @type redis_server: L{StrictRedis}
    @param wait: Wait for jobs to finish?
    @type wait: Boolean, defaults to False
    @param temp_dir: Local temporary directory for storing output for an
                     individual job.
    @type temp_dir: C{basestring}
    """

    for ix in range(len(jobids)):
        assert(jobids[ix] == joblist[ix].jobid)

    s = drmaa.Session()
    s.initialize(sid)

    if wait:
        drmaaWait = drmaa.Session.TIMEOUT_WAIT_FOREVER
    else:
        drmaaWait = drmaa.Session.TIMEOUT_NO_WAIT

    s.synchronize(jobids, drmaaWait, True)
    # print("success: all jobs finished", file=sys.stderr)
    s.exit()

    # attempt to collect results
    job_output_list = []
    for ix, job in enumerate(joblist):

        log_stdout_fn = os.path.join(temp_dir, job.name + '.o' + jobids[ix])
        log_stderr_fn = os.path.join(temp_dir, job.name + '.e' + jobids[ix])

        try:
            job_output = zload_db(redis_server, 'output{0}'.format(uniq_id),
                                   ix)
        except Exception as detail:
            print(("Error while unpickling output for gridmap job {1} from" +
                   " stored with key output_{0}_{1}").format(uniq_id, ix),
                  file=sys.stderr)
            print("This could caused by a problem with the cluster " +
                  "environment, imports or environment variables.",
                  file=sys.stderr)
            print(("Try running `{5} -m gridmap.runner {0} {1} {2} {3} " +
                   "{4}` to see if your job crashed before writing its " +
                   "output.").format(uniq_id,
                                     ix,
                                     job.path,
                                     temp_dir,
                                     gethostname(),
                                     sys.executable),
                  file=sys.stderr)
            print("Check log files for more information: ", file=sys.stderr)
            print("stdout:", log_stdout_fn, file=sys.stderr)
            print("stderr:", log_stderr_fn, file=sys.stderr)
            print("Exception: {0}".format(detail))
            sys.exit(2)

        #print exceptions
        if isinstance(job_output, Exception):
            print("Exception encountered in job with log file:",
                  file=sys.stderr)
            print(log_stdout_fn, file=sys.stderr)
            print(job_output, file=sys.stderr)
            print(file=sys.stderr)

        job_output_list.append(job_output)

    return job_output_list


def process_jobs(jobs, temp_dir='/scratch/', wait=True, white_list=None,
                 quiet=True):
    """
    Take a list of jobs and process them on the cluster.

    @param temp_dir: Local temporary directory for storing output for an
                     individual job.
    @type temp_dir: C{basestring}
    @param wait: Should we wait for jobs to finish? (Should only be false if the
                 function you're running doesn't return anything)
    @type wait: C{bool}
    @param white_list: If specified, limit nodes used to only those in list.
    @type white_list: C{list} of C{basestring}
    @param quiet: When true, do not output information about the jobs that have
                  been submitted.
    @type quiet: C{bool}
    """
    # Create new connection to Redis database with pickled jobs
    redis_server = StrictRedis(host=gethostname(), db=REDIS_DB, port=REDIS_PORT)

    # Check if Redis server is launched, and spawn it if not.
    try:
        redis_server.set('connection_test', True)
    except RedisConnectionError:
        with open('/dev/null') as null_file:
            redis_process = subprocess.Popen(['redis-server', '-'],
                                             stdout=null_file,
                                             stdin=subprocess.PIPE,
                                             stderr=null_file)
            redis_process.stdin.write('''daemonize yes
                                         pidfile {0}
                                         port {1}
                                      '''.format(os.path.join(temp_dir,
                                                              'redis{0}.pid'.format(REDIS_PORT)),
                                                 REDIS_PORT))
            redis_process.stdin.close()
            # Wait for things to get started
            sleep(5)

    # Generate random name for keys
    uniq_id = uuid.uuid4()

    # Save jobs to database
    for job_id, job in enumerate(jobs):
        zsave_db(job, redis_server, 'job{0}'.format(uniq_id), job_id)

    # Submit jobs to cluster
    sids, jobids = _submit_jobs(jobs, uniq_id, white_list=white_list,
                                temp_dir=temp_dir, quiet=quiet)

    # Reconnect and retrieve outputs
    job_outputs = _collect_jobs(sids, jobids, jobs, redis_server, uniq_id,
                                temp_dir=temp_dir, wait=wait)

    # Make sure we have enough output
    assert(len(jobs) == len(job_outputs))

    # Delete keys from existing server or just
    redis_server.delete(*redis_server.keys('job{0}_*'.format(uniq_id)))
    redis_server.delete(*redis_server.keys('output{0}_*'.format(uniq_id)))
    return job_outputs


#####################################################################
# MapReduce Interface
#####################################################################
def grid_map(f, args_list, cleanup=True, mem_free="1G", name='gridmap_job',
           num_slots=1, temp_dir='/scratch/', white_list=None,
           queue=DEFAULT_QUEUE, quiet=True):
    """
    Maps a function onto the cluster.
    @note: This can only be used with picklable functions (i.e., those that are
           defined at the module or class level).

    @param f: The function to map on args_list
    @type f: C{function}
    @param args_list: List of arguments to pass to f
    @type args_list: C{list}
    @param cleanup: Should we remove the stdout and stderr temporary files for
                    each job when we're done? (They are left in place if there's
                    an error.)
    @type cleanup: C{bool}
    @param mem_free: Estimate of how much memory each job will need (for
                     scheduling). (Not currently used, because our cluster does
                     not have that setting enabled.)
    @type mem_free: C{basestring}
    @param name: Base name to give each job (will have a number add to end)
    @type name: C{basestring}
    @param num_slots: Number of slots each job should use.
    @type num_slots: C{int}
    @param temp_dir: Local temporary directory for storing output for an
                     individual job.
    @type temp_dir: C{basestring}
    @param white_list: If specified, limit nodes used to only those in list.
    @type white_list: C{list} of C{basestring}
    @param queue: The SGE queue to use for scheduling.
    @type queue: C{basestring}
    @param quiet: When true, do not output information about the jobs that have
                  been submitted.
    @type quiet: C{bool}
    """

    # construct jobs
    jobs = [Job(f, [args] if not isinstance(args, list) else args,
                cleanup=cleanup, mem_free=mem_free,
                name='{0}{1}'.format(name, job_num), num_slots=num_slots,
                queue=queue)
            for job_num, args in enumerate(args_list)]

    # process jobs
    job_results = process_jobs(jobs, temp_dir=temp_dir, white_list=white_list,
                               quiet=quiet)

    return job_results


def pg_map(f, args_list, cleanup=True, mem_free="1G", name='gridmap_job',
           num_slots=1, temp_dir='/scratch/', white_list=None,
           queue=DEFAULT_QUEUE, quiet=True):
    """
    @deprecated: This function has been renamed grid_map.

    @param f: The function to map on args_list
    @type f: C{function}
    @param args_list: List of arguments to pass to f
    @type args_list: C{list}
    @param cleanup: Should we remove the stdout and stderr temporary files for
                    each job when we're done? (They are left in place if there's
                    an error.)
    @type cleanup: C{bool}
    @param mem_free: Estimate of how much memory each job will need (for
                     scheduling). (Not currently used, because our cluster does
                     not have that setting enabled.)
    @type mem_free: C{basestring}
    @param name: Base name to give each job (will have a number add to end)
    @type name: C{basestring}
    @param num_slots: Number of slots each job should use.
    @type num_slots: C{int}
    @param temp_dir: Local temporary directory for storing output for an
                     individual job.
    @type temp_dir: C{basestring}
    @param white_list: If specified, limit nodes used to only those in list.
    @type white_list: C{list} of C{basestring}
    @param queue: The SGE queue to use for scheduling.
    @type queue: C{basestring}
    @param quiet: When true, do not output information about the jobs that have
                  been submitted.
    @type quiet: C{bool}
    """
    return grid_map(f, args_list, cleanup=cleanup, mem_free=mem_free, name=name,
                    num_slots=num_slots, temp_dir=temp_dir,
                    white_list=white_list, queue=queue, quiet=quiet)
