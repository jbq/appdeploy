import os, sys, re, subprocess, tempfile, shutil, httplib, StringIO

def version_compare(v1, v2):
    vv1 = v1.replace('ebz_', '')
    vv2 = v2.replace('ebz_', '')
    try:
        return cmp(*zip(*map(lambda x,y:(x or 0,y or 0), map(int,vv1.split('.')), map(int,vv2.split('.')))))
    except Exception, e:
        raise Exception("Could not compare versions %s and %s: %s" % (v1, v2, repr(e)))

class DeploymentFailed(Exception):
    def __init__(self, message, originalException=None):
        Exception.__init__(self, message)
        self.originalException = originalException

    def __str__(self):
        if self.originalException:
            return "%s\n\nOriginal exception: %s" % (Exception.__str__(self), Exception.__str__(self.originalException))
        else:
            return Exception.__str__(self)

class UnknownRevision(Exception):
    pass

class ExecuteFailed(DeploymentFailed):
    pass

class BaseDeploymentProfile(object):
    def __init__(self, **kw):
        # Initialize optional fields
        self.repositoryPath = None
        self.remoteUser = None
        self.remoteDir = None
        self.revision = None
        self.dbVersionCheck = None
        self.recipient = None
        self.useRsync = False
        self.selectTag = False
        self.name = None
        self.hosts = []
        self.deploymentEngine = None

        for key in kw.keys():
            setattr(self, key, kw[key])
        self.applyConventions()

    def getRevision(self):
        """self.revision can be either a refspec or a tuple containing (function,
        arguments) to compute refspec dynamically, used eg to deploy production
        from a specific revision currently deployed in the staging environment.
        The value is computed only for the first call to getRevision().  Can be
        None if there was an error fetching the value."""
        if isinstance(self.revision, tuple):
            (fun, args) = self.revision
            try:
                self.revision = apply(fun, args)
            except UnknownRevision, e:
                self.revision = None

        return self.revision

    def getDisplayRevision(self):
        c = self.getRevision()
        if c is None:
            if self.selectTag:
                return ""
            else:
                raise DeploymentFailed("No revision provided on this deployment profile")
        return c

    def applyConventions(self):
        """ Override to apply your own conventions for guessing eg repositoryPath """
        pass

    def asdict(self):
        return {'name': self.name, 'hosts': self.hosts, 'revision': self.revision, 'revision': self.getDisplayRevision()}

class DeploymentOptions(object):
    def __init__(self):
        self.doWriteChangeLog = True
        self.doMinify = 1
        self.doNotify = 1
        self.verbose = 0
        self.forceRecipient = None
        self.skippedHosts = []
        self.skipDbVersionCheck = False
        self.skipRestart = False

class BaseDeploymentEngine(object):
    def __init__(self, profile, options):
        self.profile = profile
        self.success = 0
        self.cancelled = False
        self.options = options

    def prepare(self):
        if not(self.profile.hosts):
            raise DeploymentFailed("No deployment hosts defined")

        self.workdir = tempfile.mkdtemp(prefix=self.__class__.__name__, dir="/var/tmp")
        # XXX Git will create this directory, so remove it.  We only need a
        # unique file name in fact.
        os.rmdir(self.workdir)

        self.oldRevision = self.fetchCurrentDeployedRevision()
        repo = self.pickRepo()
        self.checkout(repo)

    def onSuccess(self):
        pass

    def run(self):
        try:
            revision = self.profile.getRevision()
            if revision is None:
                raise DeploymentFailed("No revision provided on this deployment profile")

            self.newRevision = self.reset(revision)

            if self.oldRevision is not None:
                # NOTE: Old revision can be missing if it's eg the first deployment
                print "Revision %s currently deployed" % self.oldRevision
                if self.options.doWriteChangeLog:
                    try:
                        self.writeChangeLog()
                    except ExecuteFailed:
                        # git log may fail to find the deployed revision
                        pass

            self.requestConfirmation()

            print " Deploying revision %s" % self.newRevision

            # Write revision to a text file in the work dir
            revfile = open("%s/rev.txt" % self.workdir, "w")
            revfile.write(self.newRevision)
            revfile.close()

            self.beforePush()

            # Remove the Git internals
            shutil.rmtree("%s/.git" % self.workdir)

            # Push to target environment
            self.pushToRemoteHosts()

            self.success = 1
            try:
                self.onSuccess()
            except Exception, e:
                print >> sys.stderr, "WARNING: failed to run onSuccess hook: %s" % repr(e)

        finally:
            if os.path.exists(self.workdir):
                shutil.rmtree("%s" % self.workdir)

    def confirm(self, message):
        while 1:
            a = raw_input(message)
            if a == "y" or a == "Y":
                return 1
            elif a == 'n' or a == 'N':
                return 0

    def requestConfirmation(self):
        if self.oldRevision is not None:
            cl = self.getChangeLog()
        else:
            cl = None

        print

        if self.oldRevision == self.newRevision:
            if not(self.confirm("Confirm deploying the same revision %s again? [y/n] " % self.newRevision)):
                self.cancel()
        elif cl is not None and cl != "":
            print "Need to confirm upgrading from revision %s to revision %s" % (self.oldRevision, self.newRevision)
            raw_input("Press <Enter> to review the revision log ")
            p = subprocess.Popen(["less"], stdin=subprocess.PIPE)
            try:
                p.stdin.write(cl)
            except IOError, e:
                if e.errno == 32:
                    # Broken pipe, user quit less before we could write the
                    # whole changelog
                    pass
            p.stdin.close()
            p.wait()

            if not(self.confirm("Sign-off this revision log? [y/n] ")):
                self.cancel()
        else:
            print " * WARNING * No ChangeLog Available!"

            if self.oldRevision is not None:
                if not(self.confirm("Confirm upgrading from revision %s to revision %s? [y/n] " % (self.oldRevision, self.newRevision))):
                    self.cancel()
            else:
                if not(self.confirm("Confirm deploying revision %s? [y/n] " % (self.newRevision))):
                    self.cancel()

    def cancel(self):
        print "Aborting"
        self.cancelled = True
        sys.exit(1)

    def getHosts(self):
        for host in self.profile.hosts:
            if host in self.options.skippedHosts:
                continue
            yield host

    def notify(self):
        args = ["/usr/sbin/sendmail", "-t", "-i"]
        p = subprocess.Popen(args, stdin=subprocess.PIPE)

        recipient = self.options.forceRecipient

        if not(recipient):
            recipient = self.profile.recipient

        p.stdin.write("To: %s\n" % recipient)
        p.stdin.write("Subject: Deployed %s %s to %s\n" % (self.profile.appName, self.profile.revision, self.profile.name))

        p.stdin.write("\n")
        p.stdin.write("Affected hosts: %s\n\n" % ", ".join(self.getHosts()))
        cl = self.getChangeLog()

        if self.oldRevision == self.newRevision:
            p.stdin.write("Deployed the same revision %s again" % self.newRevision)
        elif cl and cl != "":
            p.stdin.write("Upgrading from revision %s to revision %s\n\n" % (self.oldRevision, self.newRevision))
            p.stdin.write(cl)
        else:
            p.stdin.write("Upgrading to revision %s\n\n" % (self.newRevision))
            p.stdin.write("  -- No changelog available --\n")

        p.stdin.close()
        sc = p.wait()

        if sc != 0:
            raise ExecuteFailed("Command '%s' returned status code %s\n\nCommand output:\n------------------------------------------------------------------------\n%s------------------------------------------------------------------------" % (" ".join(args), sc, p.stderr.read()))

    def getChangeLog(self):
        """Get the changelog

        @return None if no changelog could be computed
        """
        file = "%s/changelog.txt" % self.workdir

        if os.path.exists(file):
            f = open(file, "r")
            c = f.read()
            f.close()
            return c
        else:
            return None

    def writeChangeLog(self):
        c = self.bexecute(["git", "log", "%s..%s" % (self.oldRevision, self.newRevision)], cwd=self.workdir)
        with open("%s/changelog.txt" % self.workdir, "w") as f:
            f.write(c)

    def pickRepo(self):
        """Pick the most relevant Git repository, depending on the availability of the requested repository on the local filesystem.

        @return path or URL to git repository suitable for issuing <tt>git clone</tt>
        """
        repo = self.profile.repositoryPath
        if not(os.path.exists(repo)):
            print "NOTE: Using a remote repository, this may be slow.  Consider maintaining a local mirror for your project."
            return repo
        return repo

    def getSource(self, host):
        return "%s/" % self.workdir

    def getDestination(self, host):
        if not(self.profile.remoteUser):
            raise DeploymentFailed("remoteUser is not set")
        if not(self.profile.remoteDir):
            raise DeploymentFailed("remoteDir is not set")

        if self.useRsync:
            return "%s@%s:%s" % (self.profile.remoteUser, host, self.profile.remoteDir)

        return "ssh://%s@%s/%s" % (self.profile.remoteUser, host, self.profile.remoteDir)

    def getSyncCommandLine(self, host):
        if self.profile.useRsync:
            args = ['rsync']
            args += self.rsyncArgs(host)
            args += self.rsyncOptions(host)
        else:
            args = ['unison']
            args += self.unisonArgs(host)
            args += self.unisonOptions(host)

        return args

    def pushToRemoteHosts(self):
        print
        print "Pushing to remote hosts"
        for host in self.getHosts():
            print " Pushing to %s" % host

            try:
                args = self.getSyncCommandLine(host)
                args.append(self.getSource(host))
                args.append(self.getDestination(host))
                self.bvexecute(args)
            except OSError, e:
                if e.errno == 2:
                    raise DeploymentFailed("useRsync=%s but %s is not installed" % (self.profile.useRsync, args[0]))
            except ExecuteFailed, e:
                raise DeploymentFailed("Failed to push %s to remote host %s" % (self.profile.appName, host), e)

        if self.options.doNotify and (self.profile.recipient or self.options.forceRecipient):
            self.notify()

        for host in self.getHosts():
            try:
                self.afterPush(host)
            except ExecuteFailed, e:
                raise DeploymentFailed("Failed to run %s after-push hook on remote host %s" % (self.profile.appName, host), e)

    def rsyncArgs(self, host):
        args = []

        if self.options.verbose:
            args.append("-i")

        args += ['-rclz', '--delete']

        return args

    def unisonArgs(self, host):
        args = []

        if self.options.verbose:
            args += ["-logfile", "/dev/stdout"]

        args += ['-batch', '-dumbtty', '-silent', '-force', self.getSource(host)]

        return args


    def rsyncOptions(self, host):
        return []

    def fetchCurrentDeployedRevisionSSH(self, host):
        try:
            return self.bexecute(["ssh", "-l", self.profile.remoteUser, host, "cat", "%s/rev.txt" % self.profile.remoteDir])
        except Exception, e:
            raise UnknownRevision(repr(e))

    @staticmethod
    def fetchCurrentDeployedRevisionHTTP(host, port, uri):
        c = httplib.HTTPConnection(host, port, timeout=5)
        try:
            c.connect()
            c.request("GET", uri)
            r = c.getresponse()
            sc = r.status
            body = r.read()
            c.close()
        except IOError, e:
            raise UnknownRevision(repr(e))

        if sc != 200:
            # Note: host can be down on purpose for the deployment, so do not
            # check status code
            raise UnknownRevision("Host %s did not provide deployed revision in return of the HTTP request" % host)

        return body.rstrip()

    def fetchCurrentDeployedRevision(self):
        last_known_rev = None

        for host in self.getHosts():
            try:
                rev = self.fetchDeployedRevision(host).rstrip()
            except UnknownRevision:
                print " * WARNING * could not get deployed revision from host %s" % host
                continue
            if last_known_rev and rev != last_known_rev:
                print " * WARNING * Web server %s has revision %s, differs from last known revision %s" % (host, rev, last_known_rev)
            last_known_rev = rev

        return last_known_rev

    def fetchDeployedRevision(self, host):
        """Override this method in a custom DeploymentEngine to provide means
        for fetching the deployed revision for the specified host"""
        return self.fetchCurrentDeployedRevisionSSH(host)

    def execute(self, args, cwd=None):
        """Execute with unbuffered stdout and stderr"""
        if self.options.verbose:
            print "Executing command: %s" % (' '.join(args))
        p = subprocess.Popen(args, cwd=cwd)
        sc = p.wait()
        if sc != 0:
            raise ExecuteFailed("Command '%s' returned status code %s" % (" ".join(args), sc))

    def bvexecute(self, args, cwd=None):
        """Execute without a result"""
        if self.options.verbose:
            self.execute(args, cwd)
            return

        self.bexecute(args, cwd)

    def bexecute(self, args, cwd=None):
        """Execute with buffered stdout and stderr and return stdout contents.  Upon error, both stdout and stderr contents are included in the exception message"""
        if self.options.verbose:
            print "Executing command: %s" % (' '.join(args))
        o = StringIO.StringIO()
        p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
        p.stdin.close()

        # Keep reading output to avoid deadlock when internal buffer is full
        while True:
            line = p.stdout.read()
            if line == "":
                break
            o.write(line)

        sc = p.wait()
        if sc != 0:
            raise ExecuteFailed("Command '%s' returned status code %s\n\nCommand output:\n------------------------------------------------------------------------\n%s------------------------------------------------------------------------\nError messages:\n------------------------------------------------------------------------\n%s------------------------------------------------------------------------" % (" ".join(args), sc, o.getvalue(), p.stderr.read()))
        return o.getvalue()

    def checkout(self, repo):
        """Checkout a Git repository

        @return Git revision number
        """
        print
        print "Checking out %s into %s" % (repo, self.workdir)
        self.bvexecute(["git", "clone", "-n", repo, self.workdir])

    def getTags(self, pattern):
        # Reverse sort by version number
        return sorted([x.rstrip() for x in self.bvexecute(["git", "tag", "-l", pattern], cwd=self.workdir).split("\n") if x], version_compare, None, True)

    def reset(self, revision):
        self.bvexecute(["git", "reset", "--hard", revision], cwd=self.workdir)
        return self.bexecute(["git", "show", "-s", "--pretty=format:%h"], cwd=self.workdir).rstrip()

    def beforePush(self):
        """Called before pushing application to any host"""
        pass

    def afterPush(self, host):
        """Called after pushing application to a single host"""
        pass

    def unisonOptions(self, host):
        pass

def getDeployment(profile, options):
    deployClass = profile.deploymentEngine
    if deployClass is None:
        deployClass = BaseDeploymentEngine
    return deployClass(profile, options)
