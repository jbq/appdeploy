import curses, time, curses.panel, textwrap, traceback, sys, os, syslog, appdeploy, getopt, collections

class UserCompleted(Exception):
    pass

class Window(object):
    def __init__(self, pw):
        self.pw = pw
        self.w = pw.derwin(1, 1)
        self.msgs = []
    def width(self):
        (maxy, maxx) = self.w.getmaxyx()
        return maxx
    def height(self):
        (maxy, maxx) = self.w.getmaxyx()
        return maxy

    def echo(self, msg, attr=0):
        self.msgs.append((msg, attr))

    def repaint(self):
        w = self.w
        pw = self.pw
        w.clear()
        #color = curses.color_pair(0)
        #w.attrset(color)
        self.prepare()
        if len(self.msgs) > self.height():
            raise Exception("Too many messages to display: %s" % len(self.msgs))
        for (msg, attr) in self.msgs[-(self.height())+1:]:
            msg = "%s" % msg
            w.addstr(str(msg), attr)
        self.decorate()
        w.noutrefresh()
        pw.noutrefresh()
        curses.doupdate()

    def reset(self):
        self.msgs = []

    def decorate(self):
        pass

    def prepare(self):
        pass

class ErrorWindow(Window):
    def __init__(self, pw):
        Window.__init__(self, pw)
        color = curses.color_pair(1)
        self.pw.attrset(color)
        self.pw.bkgd(' ', color)
        self.w.attrset(color)
        self.w.bkgd(' ', color)

    def decorate(self):
        self.pw.border()

class OptionsWindow(Window):
    def __init__(self, pw):
        Window.__init__(self, pw)
        self.selectedOption = None
        self.options = []

    def previousOption(self):
        if not(self.selectedOption):
            self.selectedOption = self.options[-1]
        else:
            index = self.options.index(self.selectedOption)
            self.selectedOption = self.options[(index - 1) % len(self.options)]
        self.repaint()

    def nextOption(self):
        if not(self.selectedOption):
            self.selectedOption = self.options[0]
        else:
            index = self.options.index(self.selectedOption)
            self.selectedOption = self.options[(index + 1) % len(self.options)]
        self.repaint()

    def addOption(self, key, msg, padding=1):
        self.options.append(key)
        f = "  %%%s.%ss    " % (padding, padding)
        self.echo(f % str(key), curses.A_BOLD)
        if self.selectedOption == key:
            attr = curses.A_STANDOUT
        else:
            attr = 0
        self.echo("%s\n" % str(msg), attr)

    def selectOption(self, key):
        self.selectedOption = key
        self.repaint()

    def validateOption(self):
        if self.selectedOption:
            self.handler(self.selectedOption)

    def availableOptions(self):
        return self.options

class WelcomeScreen(OptionsWindow):
    def __init__(self, pw, applications):
        OptionsWindow.__init__(self, pw)
        self.applications = applications

    def prepare(self):
        self.reset()
        self.echo("Welcome!\n\n", curses.A_BOLD)
        self.echo(textwrap.fill("To perform deployment, please choose one of the following options:", self.width() - 2))
        self.echo("\n\n")

        for applicationKey, applicationInfo in self.applications:
            self.addOption(applicationKey, applicationInfo['displayName'])

        self.echo("\n\n\n\nOther options:\n\n")
        self.addOption('q', "Quit deployment program")

class SelectProfileScreen(OptionsWindow):
    def __init__(self, pw, application):
        OptionsWindow.__init__(self, pw)
        self.application = application

    def prepare(self):
        self.reset()
        self.echo("%s\n\n" % self.application['displayName'], curses.A_BOLD)

        maxlen = 0
        format = "%%(name)-40.40s %%(revision)-20.20s %%(dhosts)-%s.%ss" % (maxlen, maxlen)

        for (profileKey, profile) in self.application['profiles']:
            if len(profile.hosts) == 0:
                raise appdeploy.DeploymentFailed("Please define hosts for your deployment profile with key %s" % profileKey)
            if profile.name is None:
                raise appdeploy.DeploymentFailed("Please define a name for your deployment profile with key %s" % profileKey)
            hosts = ", ".join(profile.hosts)
            if len(hosts) > maxlen:
                maxlen = len(hosts)
            format = "%(name)-40.40s %(revision)-20.20s"
            format += " %%(dhosts)-%s.%ss" % (maxlen, maxlen)

        self.echo("       ")
        self.echo(format % {'name': 'Name', 'revision': 'Revision', 'dhosts': "Hosts"})
        self.echo("\n\n")

        for (profileKey, profile) in self.application['profiles']:
            profileDict = profile.asdict()
            profileDict['dhosts'] = ", ".join(profile.hosts)
            self.addOption(profileKey, format % profileDict)

        self.echo("\n\n\n\nOther options:\n\n")
        self.addOption('q', "Return to main screen")

class SelectTagScreen(OptionsWindow):
    def __init__(self, pw, d):
        assert isinstance(d, appdeploy.Deployment)
        OptionsWindow.__init__(self, pw)
        self.d = d

    def prepare(self):
        self.reset()
        self.echo("Select a tag for %s\n\n" % self.d.profile.appName, curses.A_BOLD)

        for tag in self.d.getAllowedTags()[:10]:
            self.addOption(tag, "Tag %s" % tag, padding=20)

class UI(object):
    def initDisplay(self, screen):
        curses.use_default_colors()
        self.baseWin = screen
        # Make sure to refresh the main screen
        self.baseWin.noutrefresh()
        self.errorWindow = None
        self.windowList = collections.deque()
        curses.init_pair(1, curses.COLOR_RED, -1)

    def error(self, msg):
        ew = self.createErrorWindow()
        ew.echo(str(msg))
        ew.repaint()
        curses.beep()
        time.sleep(2)
        self.errorWindow = None
        self.refresh()

    def refresh(self):
        self.windowList[0].repaint()

    def eventLoop(self):
        while 1:
            # FIXME if I take curWin, chars have different values!
            c = self.baseWin.getch()

            curWin = self.windowList[0]
            if c == 10: # enter (newline)
                curWin.validateOption()
            elif c == curses.KEY_UP:
                curWin.previousOption()
            elif c == curses.KEY_DOWN:
                curWin.nextOption()
            elif c < 256 and chr(c) in curWin.availableOptions():
                curWin.selectOption(chr(c))
            else:
                curses.flushinp()
                self.error("Unknown key binding: %s" % c)

    def createErrorWindow(self):
        (maxy, maxx) = self.baseWin.getmaxyx()
        ew = ErrorWindow(self.baseWin.derwin(int(.5 * maxy), int(.6 * maxx), int(.25*maxy), int(.2*maxx)))
        return ew

class SelectTagUI(UI):
    def __init__(self, d):
        self.d = d

    def display(self, screen):
        UI.initDisplay(self, screen)
        w = SelectTagScreen(screen, self.d)
        w.handler = self.selectTag
        w.repaint()
        self.windowList.appendleft(w)
        self.eventLoop()

    def selectTag(self, key):
        self.d.profile.revision = key
        raise UserCompleted()

class DeploymentUI(UI):
    def parseOptions(self):
        opts, args = getopt.getopt(sys.argv[1:], 'vr:h', ['skip-dbversion', 'skip-minify', 'skip-notify', 'skip-restart', 'skip-host=', 'verbose', 'force-recipient=', 'no-changelog', 'help'])

        for o, a in opts:
            if o in ("-h", "--help"):
                print >> sys.stderr, """Usage: %s [OPTIONS]

    Available options:

        -h | --help             This very help

        -v | --verbose          Turn on verbose mode during deployment

        -r | --force-recipient  Use the specified recipient for deployment notification

        --skip-dbversion        Do not check the currently installed database
                                version against the database version required for deployment

        --skip-minify           Do not minify JS and CSS to speedup deployment

        --skip-notify           Do not send deployment notification

        --skip-restart          Do not restart impacted services upon deployment

        --skip-host             Do not deploy to the specified host.  Can be
                                specified multiple times on the command-line.

        --no-changelog          Do not bother creating the changelog
    """ % os.path.basename(sys.argv[0])
                sys.exit(0)
            if o in ("-v", "--verbose"):
                self.options.verbose = 1
            if o in ("--skip-minify"):
                self.options.doMinify = 0
            if o in ("--skip-notify"):
                self.options.doNotify = 0
            if o in ("--skip-host"):
                self.options.skippedHosts.append(a)
            if o in ("--skip-dbversion"):
                self.options.skipDbVersionCheck = True
            if o in ("--skip-restart"):
                self.options.skipRestart = True
            if o in ("-r", "--force-recipient"):
                self.options.forceRecipient = a
            if o in ("--no-changelog"):
                self.options.doWriteChangeLog = False

    def __init__(self, applications):
        self.applications = applications
        self.applicationsAsDict = {}
        for applicationKey, applicationInfo in self.applications:
            self.applicationsAsDict[applicationKey] = applicationInfo
        self.options = appdeploy.DeploymentOptions()
        self.parseOptions()

    def display(self, screen):
        UI.initDisplay(self, screen)

        w = WelcomeScreen(screen, self.applications)
        w.handler = self.performAction
        w.repaint()
        self.windowList.appendleft(w)
        try:
            self.eventLoop()
        finally:
            # Clear to make sure the next curses invocation will not refresh
            # this window
            w.w.clear()

    def selectProfile(self, key):
        displayName = self.selectedApplication['displayName']
        profiles = self.selectedApplication['profiles']
        if key == 'q':
            self.windowList.popleft()
            self.refresh()
        elif key in [p[0] for p in profiles]:
            for profile in profiles:
                if profile[0] == key:
                    selectedProfile = profile[1]
                    break
            self.deployment = appdeploy.getDeployment(selectedProfile, self.options)
            raise UserCompleted()
        else:
            self.error("No action defined for option: %s" % key)

    def showProfiles(self, application):
        w = SelectProfileScreen(self.baseWin, application)
        w.handler = self.selectProfile
        w.repaint()
        self.windowList.appendleft(w)

    def performAction(self, key):
        if key == 'q':
            sys.exit(0)
        elif key in self.applicationsAsDict.keys():
            app = self.applicationsAsDict[key]
            self.selectedApplication = app
            self.showProfiles(app)
        else:
            self.error("No key binding for: %s" % key)

class CursesStdout(object):
    def __enter__(self):
        pass

    def __exit__(self, *args):
        sys.stdout = sys.__stdout__ = os.fdopen(sys.__stdout__.fileno(), 'w', 0)

def main(applications):
    os.environ['NCURSES_NO_SETBUF'] = '1'
    syslog.openlog("deploy", syslog.LOG_PID, syslog.LOG_USER)
    ui = DeploymentUI(applications)

    try:
        curses.wrapper(ui.display)
    except UserCompleted:
        pass

    d = ui.deployment

    # Prepare deployment
    d.prepare()
    if d.profile.selectTag:
        if len(d.getAllowedTags()) == 0:
            raise appdeploy.DeploymentFailed("Cannot find any tag matching %s.*" % d.profile.branch)
        # Make sure user has seen deployment prepare output()
        print
        raw_input("Press <Enter> to continue ")
        # Run a new ncurses app to select a tag
        ui = SelectTagUI(d)

        try:
            curses.wrapper(ui.display)
        except UserCompleted:
            pass

    try:
        # Run the deployment
        d.run()
        print
        print "DEPLOYMENT SUCCESSFUL"
    except SystemExit:
        raise
    except appdeploy.DeploymentFailed, e:
        print
        print "ERROR: %s" % e
        for line in str(e).split("\n"):
            syslog.syslog(syslog.LOG_ERR, line)
    except:
        traceback.print_exc()

        for line in traceback.format_exc().split("\n"):
            syslog.syslog(syslog.LOG_ERR, line)
    finally:
        if not(d.success) and not(d.cancelled):
            print
            print "DEPLOYMENT FAILED"
            print
            syslog.syslog(syslog.LOG_ERR, "Deployment of %s to profile %s initiated by %s failed" % (d.profile.appName, d.profile.name, os.environ['LOGNAME']))
            sys.exit(1)

if __name__ == "__main__":
    main()
