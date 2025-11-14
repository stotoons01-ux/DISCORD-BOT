from setuptools import setup, find_packages

setup(
    name="whiteout-survival-bot",
    version="1.0.0",
    packages=find_packages(),
    install_requires=[
        'setuptools>=65.5.1',
        'wheel>=0.38.4',
        'discord.py==2.6.3',
        'aiohttp==3.9.0',
        'python-dotenv==1.0.0',
        'beautifulsoup4==4.12.2',
        'lxml==4.9.3',
        'asyncio==3.4.3',
        'requests==2.31.0',
        'python-dateutil==2.8.2',
        'pytz==2023.3',
    'matplotlib==3.7.3',
    'pandas==1.5.3',
        'google-api-python-client==2.108.0',
        'google-auth-httplib2==0.1.1',
        'google-auth-oauthlib==1.1.0',
        'numpy==1.24.4',
    ],
    python_requires='>=3.10,<4',
)