"""
The Souffle reasoner.
"""

import os
import psutil
from typing import Optional
from timeout_decorator import timeout, TimeoutError  # type: ignore
from bench_executor.container import Container
from bench_executor.logger import Logger

VERSION = '1.0.0'

TIMEOUT = 3 * 3600  # 3 hours



class Souffle(Container):
    """Souffle container for executing R2RML and RML mappings."""

    def __init__(self, data_path: str, config_path: str, directory: str,
                 verbose: bool):
        """Creates an instance of the Souffle class.

        Parameters
        ----------
        data_path : str
            Path to the data directory of the case.
        config_path : str
            Path to the config directory of the case.
        directory : str
            Path to the directory to store logs.
        verbose : bool
            Enable verbose logs.
        """
        self._data_path = os.path.abspath(data_path)
        self._config_path = os.path.abspath(config_path)
        self._logger = Logger(__name__, directory, verbose)
        self._verbose = verbose
        
        os.makedirs(os.path.join(self._data_path, 'souffle'), exist_ok=True)
        super().__init__(f'alloka/souffle:v{VERSION}', 'Souffle',
                         self._logger,
                         volumes=[f'{self._data_path}/souffle:/data',
                                  f'{self._data_path}/shared:/data/shared'])
        print('INIT')

    @property
    def root_mount_directory(self) -> str:
        """Subdirectory in the root directory of the case for Souffle.

        Returns
        -------
        subdirectory : str
            Subdirectory of the root directory for Souffle.

        """
        return __name__.lower()

    @timeout(TIMEOUT)

    def _execute_with_timeout(self, cmd1: str) -> bool:

        """Execute a mapping with a provided timeout.

        Returns
        -------
        success : bool
            Whether the execution was successfull or not.
        """
        self._logger.info(f'Executing Souffle with arguments '
                          f'{" ".join(arguments)}')

        # Execute command

        cmd = cmd1
        return self.run_and_wait_for_exit(cmd1)


    def execute(self, arguments: list) -> bool:
        """Execute Souffle with given arguments.

        Parameters
        ----------
        arguments : list
            Arguments to supply to Souffle.

        Returns
        -------
        success : bool
            Whether the execution succeeded or not.
        """
        try:
            return self._execute_with_timeout(arguments)
        except TimeoutError:
            msg = f'Timeout ({TIMEOUT}s) reached for Souffle'
            self._logger.warning(msg)

        return False

    def execute_mapping(self, mapping_file: str, output_file: str,serialization: str,rdb_username: Optional[str] = None,
                        rdb_password: Optional[str] = None,
                        rdb_host: Optional[str] = None,
                        rdb_port: Optional[int] = None,
                        rdb_name: Optional[str] = None,
                        rdb_type: Optional[str] = None) -> bool:

        """Execute a First Order Logic mapping file with Souffle.

        Parameters
        ----------
        mapping_file : str
            Path to the mapping file to convert.
        output_file : str
            Name of the output file to store generated triples in.

        Returns
        -------
        success : bool
            Whether the execution was successfull or not.
        """

        max_heap = int(psutil.virtual_memory().total * (1/2))

        # Execute command
        arguments1 = ['']  # Output directory
        if rdb_username is not None and rdb_password is not None \
                and rdb_host is not None and rdb_port is not None \
                and rdb_name is not None and rdb_type is not None:

            arguments1.append('-u ')
            arguments1.append(rdb_username)
            arguments1.append('-p ')
            arguments1.append(rdb_password)

            parameters = ''
            if rdb_type == 'MySQL':
                protocol = 'jdbc:mysql'
                parameters = '?allowPublicKeyRetrieval=true&useSSL=false'
            elif rdb_type == 'PostgreSQL':
                protocol = 'jdbc:postgresql'
            else:
                raise ValueError(f'Unknown RDB type: "{rdb_type}"')
            rdb_dsn = f'\'{protocol}://{rdb_host}:{rdb_port}/' + \
                      f'{rdb_name}{parameters}\''
            arguments1.append('-dsn ')
            arguments1.append(rdb_dsn)
        cmd1 = f'java -Xmx{max_heap} -Xms{max_heap}' + \
              ' -jar rulegen.jar -m '+ os.path.join('/data/shared/', mapping_file)+ \
              f'{" ".join(arguments1)}'     
        #self.run_and_wait_for_exit(cmd1)
        arguments = ['', os.path.join('/data/shared/Datalog_rules.rs'),
             ' -D', '/data/shared/']  # Output directory
        # if rdb_username is not None and rdb_password is not None \
        #         and rdb_host is not None and rdb_port is not None \
        #         and rdb_name is not None and rdb_type is not None:

        #     arguments.append('-u')
        #     arguments.append(rdb_username)
        #     arguments.append('-p')
        #     arguments.append(rdb_password)

        #     parameters = ''
        #     if rdb_type == 'MySQL':
        #         protocol = 'jdbc:mysql'
        #         parameters = '?allowPublicKeyRetrieval=true&useSSL=false'
        #     elif rdb_type == 'PostgreSQL':
        #         protocol = 'jdbc:postgresql'
        #     else:
        #         raise ValueError(f'Unknown RDB type: "{rdb_type}"')
        #     rdb_dsn = f'\'{protocol}://{rdb_host}:{rdb_port}/' + \
        #               f'{rdb_name}{parameters}\''
        #     arguments.append('-dsn')
        #     arguments.append(rdb_dsn)

        return self._execute_with_timeout(cmd1)
