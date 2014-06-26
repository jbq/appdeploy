from distutils.core import setup
setup(name='appdeploy',
      version='1.0.2',
      description="Application deployment tool I use and maintain for years, to be used in a console (uses an ncurses UI)",
      packages=['appdeploy'],
      author='Jean-Baptiste Quenot',
      author_email='jbq@caraldi.com',
      url="http://github.com/jbq/appdeploy",
      package_dir = {'': 'lib'},
      scripts=["examples/my_deploy_script"]
    )
