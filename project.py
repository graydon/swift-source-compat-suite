#!/usr/bin/env python
# ===--- project.py -------------------------------------------------------===
#
#  This source file is part of the Swift.org open source project
#
#  Copyright (c) 2014 - 2017 Apple Inc. and the Swift project authors
#  Licensed under Apache License v2.0 with Runtime Library Exception
#
#  See https://swift.org/LICENSE.txt for license information
#  See https://swift.org/CONTRIBUTORS.txt for the list of Swift project authors
#
# ===----------------------------------------------------------------------===

"""A library containing common project building functionality."""

import argparse
import os
import platform
import re
import subprocess
import shutil
import filecmp
import sys
import json
import time

import common

swift_branch = None


def set_swift_branch(branch):
    """Configure the library for a specific branch.

    >>> set_swift_branch('master')
    """
    global swift_branch
    swift_branch = branch
    common.set_swift_branch(branch)


class ProjectTarget(object):
    """An abstract project target."""

    def get_build_command(self, incremental=False):
        """Return a command that builds the project target."""
        raise NotImplementedError

    def get_test_command(self, incremental=False):
        """Return a command that tests the project target."""
        raise NotImplementedError

    def build(self, sandbox_profile, stdout=sys.stdout, stderr=sys.stderr,
              incremental=False):
        """Build the project target."""
        return common.check_execute(self.get_build_command(incremental=incremental),
                                    sandbox_profile=sandbox_profile,
                                    stdout=stdout, stderr=stdout)

    def test(self, sandbox_profile, stdout=sys.stdout, stderr=sys.stderr,
             incremental=False):
        """Test the project target."""
        return common.check_execute(self.get_test_command(incremental=incremental),
                                    sandbox_profile=sandbox_profile,
                                    stdout=stdout, stderr=stdout)


class XcodeTarget(ProjectTarget):
    """An Xcode workspace scheme."""

    def __init__(self, project, target, destination, sdk, build_settings,
                 is_workspace, has_scheme):
        self._project = project
        self._target = target
        self._destination = destination
        self._sdk = sdk
        self._build_settings = build_settings
        self._is_workspace = is_workspace
        self._has_scheme = has_scheme

    @property
    def project_param(self):
        if self._is_workspace:
            return '-workspace'
        return '-project'

    @property
    def target_param(self):
        if self._has_scheme:
            return '-scheme'
        return '-target'

    def get_build_command(self, incremental=False):
        project_param = self.project_param
        target_param = self.target_param
        build_dir = os.path.join(os.path.dirname(self._project),
                                 'build')
        build = ['clean', 'build']
        if incremental:
            build = ['build']
        dir_override = []
        if self._has_scheme:
            dir_override = ['-derivedDataPath', build_dir]
        else:
            dir_override = ['SYMROOT=' + build_dir]
        command = (['xcodebuild']
                   + build
                   + [project_param, self._project,
                      target_param, self._target,
                      '-destination', self._destination]
                   + dir_override
                   + ['-sdk', self._sdk,
                      'CODE_SIGN_IDENTITY=',
                      'CODE_SIGNING_REQUIRED=NO',
                      'ENABLE_BITCODE=NO',
                      'INDEX_ENABLE_DATA_STORE=NO',
                      'GCC_TREAT_WARNINGS_AS_ERRORS=NO'])
        for setting, value in self._build_settings.iteritems():
            if setting == 'CONFIGURATION':
                command += ['-configuration', value]
            else:
                command += ['%s=%s' % (setting, value)]

        return command

    def get_test_command(self, incremental=False):
        project_param = self.project_param
        target_param = self.target_param
        test = ['clean', 'test']
        if incremental:
            test = ['test']
        command = (['xcodebuild']
                   + test
                   + [project_param, self._project,
                      target_param, self._target,
                      '-destination', self._destination,
                      '-sdk', self._sdk,
                      # TODO: stdlib search code
                      'SWIFT_LIBRARY_PATH=%s' %
                      get_stdlib_platform_path(
                          self._build_settings['SWIFT_EXEC'],
                          self._destination)]
                   + ['INDEX_ENABLE_DATA_STORE=NO',
                      'GCC_TREAT_WARNINGS_AS_ERRORS=NO'])
        for setting, value in self._build_settings.iteritems():
            command += ['%s=%s' % (setting, value)]

        return command


def get_stdlib_platform_path(swiftc, destination):
    """Return the corresponding stdlib name for a destination."""
    platform_stdlib_path = {
        'macOS': 'macosx',
        'iOS': 'iphonesimulator',
        'tvOS': 'appletvsimulator',
        'watchOS': 'watchsimulator',
    }
    stdlib_dir = None
    for platform_key in platform_stdlib_path:
        if platform_key in destination:
            stdlib_dir = platform_stdlib_path[platform_key]
            break
    assert stdlib_dir is not None
    stdlib_path = os.path.join(os.path.dirname(os.path.dirname(swiftc)),
                               'lib/swift/' + stdlib_dir)
    return stdlib_path


def get_sdk_platform_path(destination, stdout=sys.stdout, stderr=sys.stderr):
    """Return the corresponding sdk path for a destination."""
    platform_sdk_path = {
        'Xcode': 'macosx',
        'macOS': 'macosx',
        'iOS': 'iphoneos',
        'tvOS': 'appletvos',
        'watchOS': 'watchos',
    }
    sdk_dir = None
    for platform_key in platform_sdk_path:
        if platform_key in destination:
            sdk_dir = common.check_execute_output([
                '/usr/bin/xcrun',
                '-show-sdk-path',
                '-sdk', platform_sdk_path[platform_key]
            ], stdout=stdout, stderr=stderr).strip()
            break
    assert sdk_dir, 'Unable to find SDK'
    return sdk_dir


def clean_swift_package(path, swiftc, sandbox_profile,
                        stdout=sys.stdout, stderr=sys.stderr):
    """Clean a Swift package manager project."""
    swift = os.path.join(os.path.dirname(swiftc), 'swift')
    if swift_branch == 'swift-3.0-branch':
        command = [swift, 'build', '-C', path, '--clean']
    else:
        command = [swift, 'package', '-C', path, 'clean']
    if (swift_branch not in ['swift-3.0-branch',
                             'swift-3.1-branch']):
        command.insert(2, '--disable-sandbox')
    return common.check_execute(command, sandbox_profile=sandbox_profile,
                                stdout=stdout, stderr=stderr)


def build_swift_package(path, swiftc, configuration, sandbox_profile,
                        stdout=sys.stdout, stderr=sys.stderr,
                        added_swift_flags=None,
                        incremental=False):
    """Build a Swift package manager project."""
    swift = os.path.join(os.path.dirname(swiftc), 'swift')
    if not incremental:
        clean_swift_package(path, swiftc, sandbox_profile,
                            stdout=stdout, stderr=stderr)
    env = os.environ
    env['SWIFT_EXEC'] = swiftc
    command = [swift, 'build', '-C', path, '--verbose',
               '--configuration', configuration]
    if (swift_branch not in ['swift-3.0-branch',
                             'swift-3.1-branch']):
        command.insert(2, '--disable-sandbox')
    if added_swift_flags is not None:
        for flag in ' '.join(added_swift_flags).split():
            command += ["-Xswiftc", flag]
    return common.check_execute(command, timeout=3600,
                                sandbox_profile=sandbox_profile,
                                stdout=stdout, stderr=stderr,
                                env=env)


def test_swift_package(path, swiftc, sandbox_profile,
                       stdout=sys.stdout, stderr=sys.stderr,
                       added_swift_flags=None,
                       incremental=False):
    """Test a Swift package manager project."""
    swift = os.path.join(os.path.dirname(swiftc), 'swift')
    if not incremental:
        clean_swift_package(path, swiftc, sandbox_profile)
    env = os.environ
    env['SWIFT_EXEC'] = swiftc
    command = [swift, 'test', '-C', path, '--verbose']
    if added_swift_flags is not None:
        for flag in ' '.join(added_swift_flags).split():
            command += ["-Xswiftc", flag]
    if (swift_branch not in ['swift-3.0-branch',
                             'swift-3.1-branch']):
        command.insert(2, '--disable-sandbox')
    return common.check_execute(command, timeout=3600,
                                sandbox_profile=sandbox_profile,
                                stdout=stdout, stderr=stderr,
                                env=env)


def checkout(root_path, repo, commit):
    """Checkout an indexed repository."""
    path = os.path.join(root_path, repo['path'])
    if repo['repository'] == 'Git':
        if os.path.exists(path):
            return common.git_update(repo['url'], commit, path)
        else:
            return common.git_clone(repo['url'], path, tree=commit)
    raise common.Unreachable('Unsupported repository: %s' %
                             repo['repository'])


def strip_resource_phases(repo_path, stdout=sys.stdout, stderr=sys.stderr):
    """Strip resource build phases from a given project."""
    command = ['perl', '-i', '-00ne',
               'print unless /Begin PBXResourcesBuildPhase/']
    for root, dirs, files in os.walk(repo_path):
        for filename in files:
            if filename == 'project.pbxproj':
                pbxfile = os.path.join(root, filename)
                common.check_execute(command + [pbxfile],
                                     stdout=stdout, stderr=stderr)


def dispatch(root_path, repo, action, swiftc, swift_version,
             sandbox_profile_xcodebuild, sandbox_profile_package,
             added_swift_flags, build_config, should_strip_resource_phases=False,
             stdout=sys.stdout, stderr=sys.stderr,
             incremental=False):
    """Call functions corresponding to actions."""

    if action['action'] == 'BuildSwiftPackage':
        if not build_config:
            build_config = action['configuration']
        return build_swift_package(os.path.join(root_path, repo['path']),
                                   swiftc,
                                   build_config,
                                   sandbox_profile_package,
                                   stdout=stdout, stderr=stderr,
                                   added_swift_flags=added_swift_flags,
                                   incremental=incremental)
    elif action['action'] == 'TestSwiftPackage':
        return test_swift_package(os.path.join(root_path, repo['path']),
                                  swiftc,
                                  sandbox_profile_package,
                                  stdout=stdout, stderr=stderr,
                                  added_swift_flags=added_swift_flags,
                                  incremental=incremental)
    elif re.match(r'^(Build|Test)Xcode(Workspace|Project)(Scheme|Target)$',
                  action['action']):
        match = re.match(
            r'^(Build|Test)Xcode(Workspace|Project)(Scheme|Target)$',
            action['action']
        )

        build_settings = {
            'SWIFT_EXEC': swiftc
        }

        if build_config == 'debug':
            build_settings['CONFIGURATION'] = 'Debug'
        elif build_config == 'release':
            build_settings['CONFIGURATION'] = 'Release'
        elif 'configuration' in action:
            build_settings['CONFIGURATION'] = action['configuration']

        other_swift_flags = []
        if swift_version:
            other_swift_flags += ['-swift-version', swift_version]
            build_settings['SWIFT_VERSION'] = swift_version
        if added_swift_flags:
            other_swift_flags += added_swift_flags
        if other_swift_flags:
            other_swift_flags = ['$(OTHER_SWIFT_FLAGS)'] + other_swift_flags
            build_settings['OTHER_SWIFT_FLAGS'] = ' '.join(other_swift_flags)

        is_workspace = match.group(2).lower() == 'workspace'
        project_path = os.path.join(root_path, repo['path'],
                                    action[match.group(2).lower()])
        has_scheme = match.group(3).lower() == 'scheme'
        xcode_target = \
            XcodeTarget(project_path,
                        action[match.group(3).lower()],
                        action['destination'],
                        get_sdk_platform_path(action['destination'],
                                              stdout=stdout, stderr=stderr),
                        build_settings,
                        is_workspace,
                        has_scheme)
        if should_strip_resource_phases:
            strip_resource_phases(os.path.join(root_path, repo['path']),
                                  stdout=stdout, stderr=stderr)
        if match.group(1) == 'Build':
            return xcode_target.build(sandbox_profile_xcodebuild,
                                      stdout=stdout, stderr=stderr,
                                      incremental=incremental)
        else:
            return xcode_target.test(sandbox_profile_xcodebuild,
                                     stdout=stdout, stderr=stderr,
                                     incremental=incremental)
    else:
        raise common.Unimplemented("Unknown action: %s" % action['action'])


def is_xfailed(xfail_args, compatible_version, platform, swift_branch):
    """Return whether the specified platform/swift_branch is xfailed."""
    xfail = xfail_args['compatibility'].get(compatible_version, {})
    if '*' in xfail:
        return xfail['*'].split()[0]
    if '*' in xfail.get('branch', {}):
        return xfail['branch']['*'].split()[0]
    if '*' in xfail.get('platform', {}):
        return xfail['platform']['*'].split()[0]
    if swift_branch in xfail.get('branch', {}):
        return xfail['branch'][swift_branch].split()[0]
    if platform in xfail.get('platform', {}):
        return xfail['platform'][platform].split()[0]
    return None


# Helper argparse Action to implement --build-config abbreviations
class BuildConfigAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if values == 'wmo-onone':
            setattr(namespace, self.dest, 'release')
            setattr(namespace, 'add_swift_flags',
                    (getattr(namespace, 'add_swift_flags', [])
                     + ['-wmo', '-Onone']))
        else:
            setattr(namespace, self.dest, values)


def add_arguments(parser):
    """Add common arguments to parser."""
    parser.add_argument('--verbose',
                        action='store_true')
    # TODO: remove Linux sandbox hack
    if platform.system() == 'Darwin':
        parser.add_argument('--swiftc',
                            metavar='PATH',
                            help='swiftc executable',
                            required=True,
                            type=os.path.abspath)
    else:
        parser.add_argument('--swiftc',
                            metavar='PATH',
                            help='swiftc executable',
                            required=True)
    parser.add_argument('--projects',
                        metavar='PATH',
                        required=True,
                        help='JSON project file',
                        type=os.path.abspath)
    parser.add_argument('--swift-version',
                        metavar='VERS',
                        help='Swift version mode (default: None)')
    parser.add_argument('--include-repos',
                        metavar='PREDICATE',
                        default=[],
                        action='append',
                        help='a Python predicate to determine '
                             'whether to include a repo '
                             '(example: \'path == "Alamofire"\')')
    parser.add_argument('--exclude-repos',
                        metavar='PREDICATE',
                        default=[],
                        action='append',
                        help='a Python predicate to determine '
                             'whether to exclude a repo '
                             '(example: \'path == "Alamofire"\')')
    parser.add_argument('--include-actions',
                        metavar='PREDICATE',
                        default=[],
                        action='append',
                        help='a Python predicate to determine '
                             'whether to include an action '
                             '(example: '
                             '\'action == "BuildXcodeWorkspaceScheme"\')')
    parser.add_argument('--exclude-actions',
                        metavar='PREDICATE',
                        default=[],
                        action='append',
                        help='a Python predicate to determine '
                             'whether to exclude an action '
                             '(example: '
                             '\'action == "BuildXcodeWorkspaceScheme"\')')
    parser.add_argument('--swift-branch',
                        metavar='BRANCH',
                        help='Swift branch configuration to use',
                        default='master')
    parser.add_argument('--sandbox-profile-xcodebuild',
                        metavar='FILE',
                        help='sandbox xcodebuild build and test operations '
                             'with profile',
                        type=os.path.abspath)
    parser.add_argument('--sandbox-profile-package',
                        metavar='FILE',
                        help='sandbox package build and test operations with '
                             'profile',
                        type=os.path.abspath)
    parser.add_argument("--test-incremental",
                        help='test incremental-mode over multiple commits',
                        action='store_true')
    parser.add_argument("--add-swift-flags",
                        action='append',
                        metavar="FLAGS",
                        help='add flags to each Swift invocation',
                        default=[])
    parser.add_argument("--skip-clean",
                        help='skip all git and build clean steps before '
                             'building projects',
                        action='store_true'),
    parser.add_argument("--build-config",
                        metavar="NAME",
                        choices=['debug', 'release', 'wmo-onone'],
                        dest='build_config',
                        action=BuildConfigAction,
                        help='specify "debug" or "release" to override '
                        'the build configuration in the projects.json file'
                        ' ("wmo-onone" is short for "release" combined with '
                        ' --add-swift-flags="-wmo -Onone")')


def add_minimal_arguments(parser):
    """Add common arguments to parser."""
    parser.add_argument('--verbose',
                        action='store_true')
    parser.add_argument('--projects',
                        metavar='PATH',
                        required=True,
                        help='JSON project file',
                        type=os.path.abspath)
    parser.add_argument('--include-repos',
                        metavar='PREDICATE',
                        default=[],
                        action='append',
                        help='a Python predicate to determine '
                             'whether to include a repo '
                             '(example: \'path == "Alamofire"\')')
    parser.add_argument('--exclude-repos',
                        metavar='PREDICATE',
                        default=[],
                        action='append',
                        help='a Python predicate to determine '
                             'whether to exclude a repo '
                             '(example: \'path == "Alamofire"\')')
    parser.add_argument('--include-actions',
                        metavar='PREDICATE',
                        default=[],
                        action='append',
                        help='a Python predicate to determine '
                             'whether to include an action '
                             '(example: '
                             '\'action == "BuildXcodeWorkspaceScheme"\')')
    parser.add_argument('--exclude-actions',
                        metavar='PREDICATE',
                        default=[],
                        action='append',
                        help='a Python predicate to determine '
                             'whether to exclude an action '
                             '(example: '
                             '\'action == "BuildXcodeWorkspaceScheme"\')')
    parser.add_argument('--swift-branch',
                        metavar='BRANCH',
                        help='Swift branch configuration to use',
                        default='master')


def evaluate_predicate(element, predicate):
    """Evaluate predicate in context of index element fields."""
    # pylint: disable=I0011,W0122,W0123
    for key in element:
        if isinstance(element[key], basestring):
            exec key + ' = """' + element[key] + '"""'
    return eval(predicate)


def included_element(include_predicates, exclude_predicates, element):
    """Return whether an index element should be included."""
    return (not any(evaluate_predicate(element, ep)
                    for ep in exclude_predicates) and
            (include_predicates == [] or
             any(evaluate_predicate(element, ip)
                 for ip in include_predicates)))


class Factory(object):
    @classmethod
    def factory(cls, *factoryargs):
        def init(*initargs):
            return cls(*(factoryargs + initargs))
        return init


def dict_get(dictionary, *args, **kwargs):
    """Return first value in dictionary by iterating through keys"""
    for key in args:
        try:
            return dictionary[key]
        except KeyError:
            pass
    if 'default' in kwargs:
        return kwargs['default']
    else:
        raise KeyError


def enum(*sequential, **named):
    enums = dict(zip(sequential, range(len(sequential))), **named)
    reverse = dict((value, key) for key, value in enums.iteritems())
    keys = enums.keys()
    values = enums.values()
    enums['keys'] = keys
    enums['values'] = values
    enums['reverse_mapping'] = reverse
    return type('Enum', (object,), enums)


ResultEnum = enum(
    'FAIL',
    'XFAIL',
    'PASS',
    'UPASS'
)


class Result(ResultEnum):
    def __init__(self, result, text):
        self.result = result
        self.text = text

    def __str__(self):
        return ResultEnum.reverse_mapping[self.result]


class ActionResult(Result):
    pass


class ListResult(Result):
    def __init__(self):
        self.subresults = {value: [] for value in ResultEnum.values}

    def add(self, result):
        self.subresults[result.result].append(result)

    def xfails(self):
        return self.subresults[Result.XFAIL]

    def fails(self):
        return self.subresults[Result.FAIL]

    def upasses(self):
        return self.subresults[Result.UPASS]

    def passes(self):
        return self.subresults[Result.PASS]

    def all(self):
        return [i for l in self.subresults.values() for i in l]

    @property
    def result(self):
        if self.subresults[Result.FAIL]:
            return Result.FAIL
        elif self.subresults[Result.UPASS]:
            return Result.UPASS
        elif self.subresults[Result.XFAIL]:
            return Result.XFAIL
        elif self.subresults[Result.PASS]:
            return Result.PASS
        else:
            return Result.PASS

    def __add__(self, other):
        n = self.__class__()
        n.subresults = {
            Result.__dict__[x]:
            (self.subresults[Result.__dict__[x]] +
             other.subresults[Result.__dict__[x]])
            for x in Result.__dict__ if not x.startswith('_')}
        return n


class ProjectListResult(ListResult):
    def __str__(self):
        output = ""

        xfails = [ar for pr in self.all() for ar in pr.xfails()]
        fails = [ar for pr in self.all() for ar in pr.fails()]
        upasses = [ar for pr in self.all() for ar in pr.upasses()]
        passes = [ar for pr in self.all() for ar in pr.passes()]

        if xfails:
            output += ('='*40) + '\n'
            output += 'XFailures:' '\n'
            for xfail in xfails:
                output += '  ' + xfail.text + '\n'

        if upasses:
            output += ('='*40) + '\n'
            output += 'UPasses:' + '\n'
            for upass in upasses:
                output += '  ' + upass.text + '\n'

        if fails:
            output += ('='*40) + '\n'
            output += 'Failures:' + '\n'
            for fail in fails:
                output += '  ' + fail.text + '\n'

        output += ('='*40) + '\n'
        output += 'Action Summary:' + '\n'
        output += ('     Passed: %s' % len(passes)) + '\n'
        output += ('     Failed: %s' % len(fails)) + '\n'
        output += ('    XFailed: %s' % len(xfails)) + '\n'
        output += ('    UPassed: %s' % len(upasses)) + '\n'
        output += ('      Total: %s' % (len(fails) +
                                        len(passes) +
                                        len(xfails) +
                                        len(upasses))) + '\n'
        output += '='*40 + '\n'
        output += 'Repository Summary:' + '\n'
        output += '      Total: %s' % len(self.all()) + '\n'
        output += '='*40 + '\n'
        output += 'Result: ' + Result.__str__(self) + '\n'
        output += '='*40
        return output


class ProjectResult(ListResult):
    pass


class ListBuilder(Factory):
    def __init__(self, include, exclude, verbose, subbuilder, target):
        self.include = include
        self.exclude = exclude
        self.verbose = verbose
        self.subbuilder = subbuilder
        self.target = target
        self.root_path = common.private_workspace('project_cache')

    def included(self, subtarget):
        return True

    def subtargets(self):
        return self.target

    def attach(self, subtarget):
        return (subtarget,)

    def build(self, stdout=sys.stdout):
        results = self.new_result()
        for subtarget in self.subtargets():
            if self.included(subtarget):
                (log_filename, output_fd) = self.output_fd(subtarget)
                subbuilder_result = None
                try:
                    subbuilder_result = self.subbuilder(*self.attach(subtarget)).build(
                        stdout=output_fd
                    )
                    results.add(subbuilder_result)
                finally:
                    if output_fd is not sys.stdout:
                        output_fd.close()
                        os.rename(
                            log_filename,
                            '%s_%s' % (subbuilder_result, log_filename),
                        )

        return results

    def new_result(self):
        return ListResult()

    def output_fd(self, subtarget):
        return (None, sys.stdout)


class ProjectListBuilder(ListBuilder):
    def included(self, subtarget):
        project = subtarget
        return (('platforms' not in project or
                 platform.system() in project['platforms']) and
                included_element(self.include, self.exclude, project))

    def new_result(self):
        return ProjectListResult()


class ProjectBuilder(ListBuilder):
    def included(self, subtarget):
        action = subtarget
        return included_element(self.include, self.exclude, action)

    def new_result(self):
        return ProjectResult()

    def subtargets(self):
        return self.target['actions']

    def attach(self, subtarget):
        return (self.target, subtarget)

    def output_fd(self, subtarget):
        scheme_target = dict_get(subtarget, 'scheme', 'target', default=False)
        destination = dict_get(subtarget, 'destination', default=False)
        project_identifier = dict_get(self.target, 'path', default=False) + " " + \
                             dict_get(subtarget, 'project', default="").split('-')[0]
        identifier = ': '.join(
            [subtarget['action'], project_identifier] +
            ([scheme_target] if scheme_target else []) +
            ([destination] if destination else [])
        )
        log_filename = re.sub(
            r"[^\w\_]+", "-", identifier.replace(': ', '_')
        ).strip('-').strip('_') + '.log'
        if self.verbose:
            fd = sys.stdout
        else:
            fd = open(log_filename, 'w')
        return (log_filename, fd)



class ActionBuilder(Factory):
    def __init__(self, swiftc, swift_version, swift_branch,
                 sandbox_profile_xcodebuild,
                 sandbox_profile_package,
                 added_swift_flags,
                 skip_clean, build_config,
                 project, action):
        self.swiftc = swiftc
        self.swift_version = swift_version
        self.swift_branch = swift_branch
        set_swift_branch(swift_branch)
        self.sandbox_profile_xcodebuild = sandbox_profile_xcodebuild
        self.sandbox_profile_package = sandbox_profile_package
        self.project = project
        self.action = action
        self.root_path = common.private_workspace('project_cache')
        self.current_platform = platform.system()
        self.added_swift_flags = added_swift_flags
        self.skip_clean = skip_clean
        self.build_config = build_config
        self.init()

    def init(self):
        pass

    def build(self, stdout=sys.stdout):
        self.checkout_branch(self.project['branch'],
                             stdout=stdout, stderr=stdout)
        return self.dispatch(self.project['branch'],
                             stdout=stdout, stderr=stdout)

    def checkout_branch(self, branch, stdout=sys.stdout, stderr=sys.stderr):
        self.checkout(ref=branch, ref_is_sha=False, pull_after_update=True,
                      stdout=stdout, stderr=stderr)

    def checkout_sha(self, sha, stdout=sys.stdout, stderr=sys.stderr):
        self.checkout(ref=sha, ref_is_sha=True, pull_after_update=False,
                      stdout=stdout, stderr=stderr)

    def checkout(self, ref, ref_is_sha, pull_after_update,
                 stdout=sys.stdout, stderr=sys.stderr):
        if not os.path.exists(self.root_path):
            common.check_execute(['mkdir', '-p', self.root_path],
                                 stdout=stdout, stderr=stderr)
        path = os.path.join(self.root_path, self.project['path'])
        if self.project['repository'] == 'Git':
            if os.path.exists(path):
                if ref_is_sha:
                    common.git_update(self.project['url'], ref, path,
                                      incremental=self.skip_clean,
                                      stdout=stdout, stderr=stderr)
                else:
                    if not self.skip_clean:
                        common.git_clean(path, stdout=stdout, stderr=stderr)
                    common.git_checkout(ref, path,
                                        force=True,
                                        stdout=stdout, stderr=stderr)
                if pull_after_update:
                    common.git_pull(path, stdout=stdout, stderr=stderr)
            else:
                common.git_clone(self.project['url'], path, ref,
                                 stdout=stdout, stderr=stderr)
        else:
            raise common.Unreachable('Unsupported repository: %s' %
                                     self.project['repository'])

    def dispatch(self, identifier, stdout=sys.stdout, stderr=sys.stderr):
        try:
            dispatch(self.root_path, self.project, self.action,
                     self.swiftc,
                     self.swift_version,
                     self.sandbox_profile_xcodebuild,
                     self.sandbox_profile_package,
                     self.added_swift_flags,
                     self.build_config,
                     incremental=self.skip_clean,
                     stdout=stdout, stderr=stderr)
        except common.ExecuteCommandFailure as error:
            return self.failed(identifier, error)
        else:
            return self.succeeded(identifier)

    def failed(self, identifier, error):
        if 'xfail' in self.action:
            error_str = 'XFAIL: %s: %s' % (identifier, error)
            result = ActionResult(Result.XFAIL, error_str)
        else:
            error_str = 'FAIL: %s: %s' % (identifier, error)
            result = ActionResult(Result.FAIL, error_str)
        common.debug_print(error_str)
        return result

    def succeeded(self, identifier):
        if 'xfail' in self.action:
            error_str = 'UPASS: %s: %s' % (identifier, self.action)
            result = ActionResult(Result.UPASS, error_str)
        else:
            error_str = 'PASS: %s: %s' % (identifier, self.action)
            result = ActionResult(Result.PASS, error_str)
        common.debug_print(error_str)
        return result


class CompatActionBuilder(ActionBuilder):

    def dispatch(self, identifier, stdout=sys.stdout, stderr=sys.stderr):
        try:
            dispatch(self.root_path, self.project, self.action,
                     self.swiftc,
                     self.swift_version,
                     self.sandbox_profile_xcodebuild,
                     self.sandbox_profile_package,
                     self.added_swift_flags,
                     self.build_config,
                     incremental=self.skip_clean,
                     should_strip_resource_phases=True,
                     stdout=stdout, stderr=stderr)
        except common.ExecuteCommandFailure as error:
            return self.failed(identifier, error)
        else:
            return self.succeeded(identifier)

    def build(self, stdout=sys.stdout):
        scheme_target = dict_get(self.action, 'scheme', 'target', default=False)
        identifier = ': '.join(
            [self.action['action'], self.project['path']] +
            ([scheme_target] if scheme_target else [])
        )
        for compatible_swift in self.project['compatibility'].keys()[:1]:
            if len(self.project['compatibility'][compatible_swift]['commit']) != 40:
                common.debug_print("ERROR: Commits must be 40 character SHA hashes")
                exit(1)
            self.checkout_sha(
                self.project['compatibility'][compatible_swift]['commit'],
                stdout=stdout, stderr=stdout
            )
            action_result = self.dispatch(compatible_swift,
                                          stdout=stdout, stderr=stdout)
            if action_result.result not in [ResultEnum.PASS,
                                            ResultEnum.XFAIL]:
                return action_result
        return action_result

    def failed(self, identifier, error):
        compatible_swift = identifier
        compatible_swift_message = (
            compatible_swift + '=' +
            self.project['compatibility'][compatible_swift]['commit']
        )
        bug_identifier = None
        if 'xfail' in self.action:
            bug_identifier = is_xfailed(self.action['xfail'],
                                        compatible_swift,
                                        self.current_platform,
                                        self.swift_branch)
        if bug_identifier:
            error_str = 'XFAIL: {bug}, {project}, {compatibility}, {action_target}'.format(
                            bug=bug_identifier,
                            project=self.project['path'],
                            compatibility=compatible_swift,
                            action_target = dict_get(self.action, 'scheme', 'target', default="Swift Package")
                        )
            if 'destination' in self.action:
                error_str += ', ' + self.action['destination']
            result = ActionResult(Result.XFAIL, error_str)
        else:
            error_str = 'FAIL: {project}, {compatibility}, {action_target}'.format(
                            project=self.project['path'],
                            compatibility=compatible_swift,
                            action_target = dict_get(self.action, 'scheme', 'target', default="Swift Package")
                        )
            if 'destination' in self.action:
                error_str += ', ' + self.action['destination']
            error_str += ', ' + str(error)
            result = ActionResult(Result.FAIL, error_str)
        common.debug_print(error_str)
        return result

    def succeeded(self, identifier):
        compatible_swift = identifier
        compatible_swift_message = (
            compatible_swift + '=' +
            self.project['compatibility'][compatible_swift]['commit']
        )
        bug_identifier = None
        if 'xfail' in self.action:
            bug_identifier = is_xfailed(self.action['xfail'],
                                        compatible_swift,
                                        self.current_platform,
                                        self.swift_branch)
        if bug_identifier:
            error_str = 'UPASS: {bug}, {project}, {compatibility}, {action_target}'.format(
                            bug=bug_identifier,
                            project=self.project['path'],
                            compatibility=compatible_swift,
                            action_target = dict_get(self.action, 'scheme', 'target', default="Swift Package")
                        )
            if 'destination' in self.action:
                error_str += ', ' + self.action['destination']
            result = ActionResult(Result.UPASS, error_str)
        else:
            error_str = 'PASS: {project}, {compatibility}, {action_target}'.format(
                            project=self.project['path'],
                            compatibility=compatible_swift,
                            action_target = dict_get(self.action, 'scheme', 'target', default="Swift Package")
                        )
            if 'destination' in self.action:
                error_str += ', ' + self.action['destination']
            result = ActionResult(Result.PASS, error_str)
        common.debug_print(error_str)
        return result

class EarlyExit(Exception):
    def __init__(self, value):
        self.value = value
    def __str__(self):
        return repr(self.value)

def ignore_missing(f):
    if (f.endswith('.dia') or
        f.endswith('~')):
        return True
    return False

def ignore_diff(f):
    if (f.endswith('-master.swiftdeps') or
        f.endswith('dependency_info.dat')):
        return True
    return False

def have_same_trees(full, incr, d):
    ok = True
    for f in d.left_only:
        if ignore_missing(f):
            continue
        ok = False
        common.debug_print("Missing 'incr' file: %s"
                           % os.path.relpath(os.path.join(d.left, f), full))

    for f in d.right_only:
        if ignore_missing(f):
            continue
        ok = False
        common.debug_print("Missing 'full' file: %s"
                           % os.path.relpath(os.path.join(d.right, f), incr))

    for f in d.diff_files:
        if ignore_diff(f):
            continue
        ok = False
        common.debug_print("File difference: %s"
                           % os.path.relpath(os.path.join(d.left, f), full))

    for sub in d.subdirs.values():
        ok = have_same_trees(full, incr, sub) and ok
    return ok


class IncrementalActionBuilder(ActionBuilder):

    def __init__(self, swiftc, swift_version, swift_branch,
                 sandbox_profile_xcodebuild,
                 sandbox_profile_package,
                 added_swift_flags, build_config,
                 project, action):
        super(IncrementalActionBuilder,
              self).__init__(swiftc, swift_version, swift_branch,
                             sandbox_profile_xcodebuild,
                             sandbox_profile_package,
                             added_swift_flags,
                             skip_clean=True,
                             build_config=build_config,
                             project=project,
                             action=action)
        self.proj_path = os.path.join(self.root_path, self.project['path'])
        self.incr_path = self.proj_path + "-incr"

    def curr_build_state_path(self):
        if self.action['action'] == 'BuildSwiftPackage':
            return os.path.join(self.proj_path, ".build")
        match = re.match(r'^(Build|Test)Xcode(Workspace|Project)(Scheme|Target)$',
                      self.action['action'])
        if match:
            project_path = os.path.join(self.proj_path,
                                        self.action[match.group(2).lower()])
            return os.path.join(os.path.dirname(project_path), "build")
        else:
            raise Exception("Unsupported action: " + self.action['action'])

    def ignored_differences(self):
        if self.action['action'] == 'BuildSwiftPackage':
            return ['ModuleCache', 'build.db', 'master.swiftdeps', 'master.swiftdeps~']
        elif re.match(r'^(Build|Test)Xcode(Workspace|Project)(Scheme|Target)$',
                      self.action['action']):
            return ['ModuleCache', 'Logs', 'info.plist', 'dgph', 'dgph~',
                    'master.swiftdeps', 'master.swiftdeps~']
        else:
            raise Exception("Unsupported action: " + self.action['action'])

    def expect_determinism(self):
        # We're not seeing determinism in incremental builds yet, so
        # for the time being disable the expectation.
        return False

    def saved_build_state_path(self, seq, flav, sha):
        return os.path.join(self.incr_path, ("build-state-%03d-%s-%.7s" %
                                             (seq, flav, sha)))

    def restore_saved_build_state(self, seq, flav, sha, stdout=sys.stdout):
        src = self.saved_build_state_path(seq, flav, sha)
        dst = self.curr_build_state_path()
        proj = self.project['path']
        common.debug_print("Restoring %s build-state #%d of %s from %s" %
                           (flav, seq, proj, src), stderr=stdout)
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst, symlinks=True)

    def save_build_state(self, seq, flav, sha, stdout=sys.stdout):
        src = self.curr_build_state_path()
        dst = self.saved_build_state_path(seq, flav, sha)
        proj = self.project['path']
        common.debug_print("Saving %s state #%d of %s to %s" %
                           (flav, seq, proj, dst), stderr=stdout)
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst, symlinks=True)

    def check_full_vs_incr(self, seq, sha, stdout=sys.stdout):
        full = self.saved_build_state_path(seq, 'full', sha)
        incr = self.saved_build_state_path(seq, 'incr', sha)
        common.debug_print("Comparing dirs %s vs. %s" % (os.path.relpath(full),
                                                         os.path.basename(incr)),
                           stderr=stdout)
        d = filecmp.dircmp(full, incr, self.ignored_differences())
        if not have_same_trees(full, incr, d):
            message = ("Dirs differ: %s vs. %s" %
                       (os.path.relpath(full),
                        os.path.basename(incr)))
            if self.expect_determinism():
                raise EarlyExit(ActionResult(Result.FAIL, message))
            else:
                common.debug_print(message, stderr=stdout)

    def excluded_by_limit(self, limits):
        for (kind, value) in limits.items():
            if self.action.get(kind) != value:
                return True
        return False

    def build(self, stdout=sys.stdout):
        action_result = ActionResult(Result.PASS, "")
        try:
            if 'incremental' in self.project:
                for vers in self.project['incremental']:
                    incr = self.project['incremental'][vers]
                    if 'limit' in incr and self.excluded_by_limit(incr['limit']):
                        continue
                    ident = "%s-incr-%s" % (self.project['path'], vers)
                    action_result = self.build_incremental(ident,
                                                           incr['commits'],
                                                           stdout=stdout)
        except EarlyExit as error:
            action_result = error.value
        return action_result

    def dispatch(self, identifier, incremental, stdout=sys.stdout, stderr=sys.stderr):
        try:
            dispatch(self.root_path, self.project, self.action,
                     self.swiftc,
                     self.swift_version,
                     self.sandbox_profile_xcodebuild,
                     self.sandbox_profile_package,
                     self.added_swift_flags,
                     self.build_config,
                     should_strip_resource_phases=False,
                     stdout=stdout, stderr=stderr,
                     incremental=incremental)
        except common.ExecuteCommandFailure as error:
            return self.failed(identifier, error)
        else:
            return self.succeeded(identifier)

    def dispatch_or_raise(self, identifier, incremental,
                          stdout=sys.stdout, stderr=sys.stderr):
        time.sleep(2)
        action_result = self.dispatch(identifier, incremental=incremental,
                                      stdout=stdout, stderr=stderr)
        time.sleep(2)
        if action_result.result not in [ResultEnum.PASS,
                                        ResultEnum.XFAIL]:
            raise EarlyExit(action_result)
        return action_result

    def build_incremental(self, identifier, commits, stdout=sys.stdout):
        if os.path.exists(self.incr_path):
            shutil.rmtree(self.incr_path)
        os.makedirs(self.incr_path)
        prev = None
        seq = 0
        action_result = ActionResult(Result.PASS, "")
        for sha in commits:
            proj = self.project['path']
            ident = "%s-%03d-%.7s" % (identifier, seq, sha)
            if prev is None:
                common.debug_print("Doing full build #%03d of %s: %.7s" %
                                   (seq, proj, sha), stderr=stdout)
                self.checkout_sha(sha, stdout=stdout, stderr=stdout)
                action_result = self.dispatch_or_raise(ident, incremental=False,
                                                       stdout=stdout, stderr=stdout)
                self.save_build_state(seq, 'full', sha, None, stdout=stdout)
            else:
                common.debug_print("Doing incr build #%d of %s: %.7s -> %.7s" %
                                   (seq, proj, prev, sha), stderr=stdout)
                common.git_checkout(sha, self.proj_path, stdout=stdout, stderr=stdout)
                common.git_submodule_update(self.proj_path, stdout=stdout, stderr=stdout)
                action_result = self.dispatch_or_raise(ident, incremental=True,
                                                       stdout=stdout, stderr=stdout)
                self.save_build_state(seq, 'incr', sha, stdout=stdout)
            prev = sha
            seq += 1
        return action_result
