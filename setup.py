from setuptools import setup


setup(
    name='project-stats',
    version='0.2.1',
    description='keep track of all your projects',
    long_description=open('README.rst').read(),
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
