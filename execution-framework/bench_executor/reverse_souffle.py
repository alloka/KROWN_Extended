"""
Reverse Souffle runner.

This resource keeps the forward Souffle runner unchanged and provides
an explicit reverse pipeline:
1) run rulegen.jar on the mapping file to generate forward Datalog,
2) run reverseR2RML.py to generate reverse Datalog (or forward+reverse
    artifacts for selective provenance),
3) run Souffle on the reverse Datalog using RDF inputs from /data/shared.
"""

import os
import psutil
import threading
from typing import Optional
from bench_executor.container import Container
from bench_executor.logger import Logger

VERSION = '1.0.0'
TIMEOUT = 3 * 3600  # 3 hours


class ReverseSouffle(Container):
    """Souffle container for reverse R2RML execution."""

    def __init__(self, data_path: str, config_path: str, directory: str,
                 verbose: bool):
        self._data_path = os.path.abspath(data_path)
        self._config_path = os.path.abspath(config_path)
        self._logger = Logger(__name__, directory, verbose)
        self._verbose = verbose

        os.makedirs(os.path.join(self._data_path, 'souffle'), exist_ok=True)
        super().__init__(f'alloka/souffle:v{VERSION}', 'ReverseSouffle',
                         self._logger,
                         volumes=[f'{self._data_path}/souffle:/data',
                                  f'{self._data_path}/shared:/data/shared'])

    @property
    def root_mount_directory(self) -> str:
        return __name__.lower()

    def _execute_with_timeout(self, command: str) -> bool:
        self._logger.info(f'Executing ReverseSouffle command: {command}')
        result = [False]
        exc_box = [None]

        def _run():
            try:
                result[0] = self.run_and_wait_for_exit(command)
            except Exception as exc:
                exc_box[0] = exc

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(TIMEOUT)
        if t.is_alive():
            self._logger.warning(f'Timeout ({TIMEOUT}s) reached for ReverseSouffle')
            return False
        if exc_box[0] is not None:
            raise exc_box[0]
        return result[0]

    def execute(self, arguments: list) -> bool:
        command = ' '.join(arguments)
        return self._execute_with_timeout(command)

    def execute_mapping(self, mapping_file: str, output_file: str,
                        serialization: str,
                        reverse_program_file: str = 'Datalog_reverse.rs',
                        forward_program_file: str = 'Datalog_forward_with_prov.rs',
                        support_report: Optional[str] = None,
                        with_provenance: bool = False,
                        target_triples_file: Optional[str] = None,
                        rdb_username: Optional[str] = None,
                        rdb_password: Optional[str] = None,
                        rdb_host: Optional[str] = None,
                        rdb_port: Optional[int] = None,
                        rdb_name: Optional[str] = None,
                        rdb_type: Optional[str] = None) -> bool:
        """Generate and execute reverse Datalog using Souffle.

        Parameters
        ----------
        mapping_file : str
            Input mapping file path relative to /data/shared.
        output_file : str
            Kept for compatibility with the execution framework metadata schema.
        serialization : str
            Kept for compatibility with the execution framework metadata schema.
        reverse_program_file : str
            Output reverse Datalog file path relative to /data/shared.
        forward_program_file : str
            Output forward/provenance Datalog file path relative to
            /data/shared when selective provenance is enabled.
        support_report : str, optional
            Optional JSON report output path relative to /data/shared.
        with_provenance : bool
            Enable provenance relations in the reverse program. Defaults to
            False (normal reverse mode).
        target_triples_file : str, optional
            Optional tab-separated file (s, p, o) relative to /data/shared.
            When provided, reverse generation is routed through
            ``--mode forward --with-provenance --reverse-output`` so
            provenance is materialized only for listed triples.
        """
        del output_file  # currently unused in reverse mode
        del serialization  # currently unused in reverse mode

        max_heap = int(psutil.virtual_memory().total * 0.5)

        mapping_path = f"/data/shared/{mapping_file.replace('\\', '/').lstrip('/')}"
        forward_program_path = '/data/shared/Datalog_rules.rs'
        reverse_program_path = (
            f"/data/shared/{reverse_program_file.replace('\\', '/').lstrip('/')}"
        )
        forward_program_path_out = (
            f"/data/shared/{forward_program_file.replace('\\', '/').lstrip('/')}"
        )

        rulegen_args: list[str] = []
        if rdb_username is not None and rdb_password is not None \
                and rdb_host is not None and rdb_port is not None \
                and rdb_name is not None and rdb_type is not None:
            rulegen_args.extend(['-u', rdb_username, '-p', rdb_password])

            parameters = ''
            if rdb_type == 'MySQL':
                protocol = 'jdbc:mysql'
                parameters = '?allowPublicKeyRetrieval=true&useSSL=false'
            elif rdb_type == 'PostgreSQL':
                protocol = 'jdbc:postgresql'
            else:
                raise ValueError(f'Unknown RDB type: "{rdb_type}"')

            rdb_dsn = f"'{protocol}://{rdb_host}:{rdb_port}/{rdb_name}{parameters}'"
            rulegen_args.extend(['-dsn', rdb_dsn])

        rulegen_suffix = ''
        if rulegen_args:
            rulegen_suffix = ' ' + ' '.join(rulegen_args)

        rulegen_cmd = (
            f'java -Xmx{max_heap} -Xms{max_heap} -jar rulegen.jar '
            f'-m "{mapping_path}"{rulegen_suffix}'
        )

        if target_triples_file:
            if not with_provenance:
                raise ValueError(
                    'target_triples_file requires with_provenance=True '
                    '(reverseR2RML requires --mode forward --with-provenance)'
                )

            target_path = (
                f"/data/shared/{target_triples_file.replace('\\', '/').lstrip('/')}"
            )
            reverse_cmd = (
                'python3 /souffle/reverseR2RML.py '
                f'"{forward_program_path}" "{forward_program_path_out}" '
                '--mode forward --with-provenance '
                f'--reverse-output "{reverse_program_path}" '
                f'--target-triples-file "{target_path}"'
            )
        else:
            reverse_cmd = (
                'python3 /souffle/reverseR2RML.py '
                f'"{forward_program_path}" "{reverse_program_path}" --mode reverse'
            )
            if with_provenance:
                reverse_cmd += ' --with-provenance'

        if support_report:
            support_path = f"/data/shared/{support_report.replace('\\', '/').lstrip('/')}"
            reverse_cmd += f' --support-report "{support_path}"'

        # Compile and execute the generated forward provenance program first
        # so reverse can consume provenance facts as input evidence.
        forward_exec_path = os.path.splitext(forward_program_path_out)[0]
        forward_souffle_cmd = (
            f'cd /data/shared && souffle -L /souffle/lib -l functors -c '
            f'"{forward_program_path_out}" -F /data/shared -D /data/shared && '
            f'"{forward_exec_path}"'
        )

        # Map forward provenance outputs to the filenames expected by reverse.
        # If a provenance file is absent, create an empty placeholder so
        # Souffle .input has a concrete file to read.
        provenance_bridge_cmd = (
            'cd /data/shared && '
            'if [ -f ExplainContributor.facts ]; then cp ExplainContributor.facts ProvContributor.csv; '
            'else : > ProvContributor.csv; fi && '
            'if [ -f ExplainQuadContributor.facts ]; then cp ExplainQuadContributor.facts ProvQuadContributor.csv; '
            'else : > ProvQuadContributor.csv; fi'
        )

        # Compile the generated reverse program, then execute the compiled
        # binary so it actually emits output facts.
        reverse_exec_path = os.path.splitext(reverse_program_path)[0]
        reverse_souffle_cmd = (
            f'cd /data/shared && souffle -L /souffle/lib -l functors -c '
            f'"{reverse_program_path}" -F /data/shared -D /data/shared && '
            f'"{reverse_exec_path}"'
        )

        if with_provenance:
            full_cmd = (
                f'bash -lc "{rulegen_cmd} && {reverse_cmd} && '
                f'{forward_souffle_cmd} && {provenance_bridge_cmd} && '
                f'{reverse_souffle_cmd}"'
            )
        else:
            full_cmd = f'bash -lc "{rulegen_cmd} && {reverse_cmd} && {reverse_souffle_cmd}"'

        return self._execute_with_timeout(full_cmd)
