from setuptools import setup
import os
import re

DIRNAME = os.path.abspath(os.path.dirname(__file__))
rel = lambda *parts: os.path.abspath(os.path.join(DIRNAME, *parts))

README = open(rel('README.rst')).read()
MAIN = open(rel('project_stats.py')).read()
VERSION = re.search("__version__ = '([^']+)'", MAIN).group(1)


setup(
    name='project-stats',
    version=VERSION,
    description='keep track of all your projects',
    long_description=README,
    url='https://github.com/xi/project-stats',
    author='Tobias Bengfort',
    author_email='tobias.bengfort@posteo.de',
    py_modules=['project_stats'],
    install_requires=[
        'python-dateutil',
        'filecachetools>=0.1.0',
        'requests',
        'pyyaml',
    ],
    extras_require={
        'cheesecake': ['cheesecake'],
    },
    entry_points={'console_scripts': [
        'project-stats=project_stats:main',
    ]},
    license='GPLv2+',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Environment :: Console',
        'Intended Audience :: Developers',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'License :: OSI Approved :: GNU General Public License v2 or later '
            '(GPLv2+)',
        'Topic :: Utilities',
    ])
