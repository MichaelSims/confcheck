#!/usr/bin/env python

"""Confcheck script

This script is part of a larger system designed to manage configuration
files for unix servers. This script is designed to be used in conjunction
with a git configuration repo which contains configuration source files
and a Makefile. The Makefile contains rules to update the configuration
of the system (or various applications) by copying the configuration source
files, setting the proper permissions, restarting system daemons, etc.
The Makefile also contains a sanity checking rule to make sure that various
conditions have been met before the system/application configuration is
updated. This script is used as a part of that sanity checking rule, which
means that it should rarely, if ever, be called directly.

This script performs various checks which will be described below:

The git configuration repo has a separate directory that corresponds with
each server that it stores config info for. This directory is named after
the fully qualified hostname (`hostname -f`) of the server in question.
Configuration files for that server are stored under that directory. If
the optional argument is passed, it will be used to check that the user
is in the proper directory for the server they are attempting to update.
This argument is an integer which describes where the "current host directory"
is in relation to the present working directory, in number of parent directory
levels. In other words, if the "current host directory" is "../../" this
argument will be 2. If the argument is 0 then the "current host directory"
is the present working directory. If this argument is supplied the script
will verify that "current host directory" == `hostname -f`.  If it isn't,
it will display a warning and return non-zero.

In addition to the above check, the script reads in a checklist file
(CONFCHECKLIST) from the current directory to perform additional tests.
The checklist file should contain comma separated filename pairs on each line.
The first file in the pair is the local config file (git repo version)
and the second is the target file (live version). This script processes
each entry and performs the following checks:

 -make sure the local file exists
 -make sure the target file exists
 -make sure the target file is the same as the latest git revision of
  its counterpart

Based on what it finds (and user input), the script will either return zero
to the shell indicating success, or non-zero indicated failure.

The script can also act in "diff mode" when passed "-d" as the only argument.
In this mode it will display diffs between the local config source files and
the target files.

The script can also update the versioned file that contains the package list
(generated from "/usr/bin/dpkg --get-selections *") for the current server
when passed "-p" as the only argument.

This script reads configuration directives from /etc/confcheck.conf.
"""
import sys
import argparse
import ConfigParser
import os.path
import logging
import subprocess
import socket
import re
import shutil


SUCCESS = 0
FAILURE = 2


def main():
    hostname = socket.gethostname()
    checklist_file_path = './CONFCHECKLIST'

    # Parse command line arguments and configure logging
    args = parse_command_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    # Validate and read config file
    if not os.path.isfile(args.config_path):
        print "Config path %s doesn't exist" % args.config_path
        return FAILURE

    cfg_working_copy_dir = 'working-copy-dir'
    cfg_repo_url = 'repo-url'
    cfg_username = 'username'
    config = ConfigParser.SafeConfigParser({cfg_working_copy_dir: '/var/cache/confcheck',
                                            cfg_repo_url: '/opt/git/confcheck',
                                            cfg_username: 'confcheck'})
    config.read(args.config_path)

    cfg_default_section = 'git'
    working_copy_dir = os.path.abspath(config.get(cfg_default_section, cfg_working_copy_dir))
    repo_url = config.get(cfg_default_section, cfg_repo_url)
    username = config.get(cfg_default_section, cfg_username)

    # Make sure working copy directory exists and that our git user can write to it
    if not os.path.isdir(working_copy_dir):
        print "Working copy path %s doesn't exist or isn't a directory" % working_copy_dir
        return FAILURE

    run_command(['chown', '-R', username, working_copy_dir])

    # Checkout repo or update it if it already exists
    repo = is_git_repo(working_copy_dir)
    if repo:
        logging.debug('%s is a git repo', working_copy_dir)
        run_command("sudo -u %s sh -c \"cd %s && git pull\"" % (username, working_copy_dir), shell=True)
    else:
        logging.debug('%s is not a git repo', working_copy_dir)
        run_command("sudo -u %s sh -c \"git clone %s %s\"" % (username, repo_url, working_copy_dir), shell=True)

    # Update package list for this server if option was passed
    if args.package_list_mode:
        return update_package_list(hostname, working_copy_dir, username)

    # Verify that the "current host directory" is correct if argument was passed
    if args.dir_level:
        path_parts = os.getcwd().split(os.path.sep)
        path_parts.reverse()
        current_host_dir = path_parts[args.dir_level]
        if not current_host_dir == hostname:
            prompt = "WARNING! Your current host directory, %s, doesn't match your " % current_host_dir
            prompt += "server's fully qualified hostname, %s. Continue anyway [y/N]? " % hostname
            response = prompt_user(prompt, 'yN')
            if not response == 'y':
                return 2

    # Read checklist file
    if not os.path.isfile(checklist_file_path):
        print "Checklist file %s doesn't exist" % checklist_file_path
        return 2

    # Determine the relative path in the working copy that corresponds to the current
    # directory. Assume the base path is called "conf", and take only what comes after that.
    module_path = re.sub(r'^.*?/conf/', '', os.getcwd())

    # Process each file pair in checklist
    for source, target in read_checklist(checklist_file_path):
        logging.debug("Source is %s, target is %s", source, target)

        if not os.path.isfile(source):
            print "Config source file %s referenced in %s doesn't exist; " \
                  "please correct this and try again." % (source, checklist_file_path)
            return FAILURE

        if not os.path.isfile(target):
            if prompt_user("Target file %s doesn't exist. Continue [Y/n]? " % target, 'Yn') == 'n':
                return FAILURE
            else:
                continue  # Skip remaining checks for this file

        # If we are running in "diff mode" display a diff between each source file and the target file
        if args.diff_mode:
            prompt = "File %s differs from %s. Display diff [Y/n]? " % (target, source)
            different = not run_command(['diff', '-q', target, source], abort_on_failure=False)
            if different and prompt_user(prompt, 'Yn') == 'y':
                run_command(['clear'], shell=True, display_output=True)
                run_command('diff -u "%s" "%s" | less' % (target, source), shell=True, display_output=True,
                            abort_on_failure=False)
            continue  # Skip remaining checks for this file

        # Check to see if it's different from the current version in git
        while True:
            file_path_in_working_copy = "%s/%s/%s" % (working_copy_dir, module_path, source)
            if not os.path.isfile(file_path_in_working_copy):
                print "%s doesn't appear to exist in version control, aborting" % source
                return FAILURE

            if run_command(['diff', '-q', file_path_in_working_copy, target], abort_on_failure=False):
                logging.debug("%s is the same as git version, skipping", target)
                break  # File are the same, proceed

            prompt = "WARNING! Target file %s has been modified since last git checkin.\n" \
                     "Continue anyway [y/N/(C)opy target to local dir/(V)iew diff] " \
                     "(Default: N)? " % target
            response = prompt_user(prompt, 'yNcv')

            if response == 'n':
                return FAILURE
            elif response == 'y':
                break
            elif response == 'v':
                run_command(['clear'], shell=True, display_output=True)
                command = "diff -u %s %s | less" % (file_path_in_working_copy, target)
                print "Executing %s..." % command
                run_command(command, shell=True, abort_on_failure=False, display_output=True)
            elif response == 'c':
                shutil.copyfile(target, source)

    return SUCCESS


def update_package_list(hostname, working_copy_dir, username):
    package_list_file = 'dpkg-selections'
    current_host_dir = "%s/%s" % (working_copy_dir, hostname)
    if not os.path.isdir(current_host_dir):
        print "Current host directory %s doesn't exist" % current_host_dir
        return FAILURE

    # Update package list file
    dpkg = '/usr/bin/dpkg'
    command = "%s --get-selections \\* > %s/conf/%s" % (dpkg, current_host_dir, package_list_file)
    if not (os.path.isfile(dpkg) and os.access(dpkg, os.X_OK)):
        # RedHat? hope so
        package_list_file = 'package.list'
        command = "/bin/rpm -qa --queryformat \"%%{NAME}.%%{ARCH}.%%{VERSION}\\n\" > %s/conf/%s" \
                  % (current_host_dir, package_list_file)
    run_command(command, shell=True)

    # Add, commit, and push the file
    run_command('sudo -u %s sh -c "git add %s"' % (username, package_list_file), shell=True)
    run_command('sudo -u %s sh -c "git commit -m \"Auto-commit by confcheck running on %s\""' % (username, hostname),
                shell=True)
    run_command('sudo -u %s sh -c "git push"' % username, shell=True)


def read_checklist(checklist_file_path):
    separator = ','
    checklist = list()
    with open(checklist_file_path) as checklist_file:
        for line in checklist_file:
            line = re.sub(r'#.*$', '', line)  # Trim comments
            line = line.strip()
            if separator in line:
                source, target = line.split(separator, 2)
                if source and target:
                    checklist.append((source, target))
    return checklist


def prompt_user(prompt, allowed_responses_string):
    allowed_responses = set()
    default_response = None
    for item in list(allowed_responses_string):
        if item == item.upper():
            default_response = item
        allowed_responses.add(item.lower())

    response = None
    while True:
        response = raw_input(prompt).strip().lower()
        if not response and default_response:
            response = default_response
            break
        response = response[0]
        if response in allowed_responses:
            break

        print "Your response was not recognized; please try again."

    return response.lower()


def is_git_repo(path):
    command = run_command(['git', 'rev-parse'], cwd=path, abort_on_failure=False)
    return command


def dump_output(*args):
    sys.stdout.write(''.join([item for item in args if item]))


class ExternalCommandFailure(Exception):
    def __init__(self, return_code):
        self.return_code = return_code

    def __str__(self):
        return repr(self.return_code)


def run_command(*args, **kwargs):
    # Grab "extra" keyword args (wish I knew a better way to do this)
    display_output = False
    abort_on_failure = True
    display_output_key = 'display_output'
    abort_on_failure_key = 'abort_on_failure'

    if display_output_key in kwargs:
        display_output = kwargs[display_output_key]
        del kwargs[display_output_key]

    if abort_on_failure_key in kwargs:
        abort_on_failure = kwargs[abort_on_failure_key]
        del kwargs[abort_on_failure_key]

    kwargs['stderr'] = subprocess.PIPE
    kwargs['stdout'] = subprocess.PIPE

    # Log command
    command_parts = args[0] if type(args[0]) is list else list(args)
    command_str = " ".join(command_parts)
    logging.debug("Running command '%s' with keyword args %s (CWD: %s)", command_str, kwargs, os.getcwd())

    # Run command and grab stdout, stderr, and return code
    process = subprocess.Popen(*args, **kwargs)
    output, error_output = process.communicate()
    return_code = process.returncode

    # Display output
    if display_output:
        dump_output(error_output, output)

    # Abort if necessary
    was_successful = return_code == 0
    if not was_successful:
        error_msg = "Command '%s' failed, return value was %s"
        logging.debug(error_msg, command_str, return_code)
        if abort_on_failure:
            print error_msg % (command_str, return_code)
            dump_output(error_output, output)
            raise ExternalCommandFailure(return_code)

    return was_successful


def parse_command_args():
    parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter, description=__doc__)
    parser.add_argument('-D', '--dir-level', metavar='<n>', type=int, help="current host directory level (see above)")
    parser.add_argument('-d', '--diff-mode', action='store_true',
                        help="display differences between versioned and actual config files")
    parser.add_argument('-p', '--package-list-mode', action='store_true', help="update dpkg list")
    parser.add_argument('-c', '--config-path', metavar='<path>', default='/etc/confcheck.conf',
                        help="optional configuration file path")
    parser.add_argument('-v', '--verbose', action='store_true', help="verbose logging output")
    args = parser.parse_args()
    return args


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ExternalCommandFailure as e:
        sys.exit(e.return_code)
