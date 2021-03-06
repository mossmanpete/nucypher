"""
This file is part of nucypher.

nucypher is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

nucypher is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with nucypher.  If not, see <https://www.gnu.org/licenses/>.

"""

import os
import shutil

import click
from nacl.exceptions import CryptoError
from twisted.internet import stdio
from twisted.logger import Logger
from twisted.logger import globalLogPublisher

from constant_sorrow.constants import NO_BLOCKCHAIN_CONNECTION, NO_PASSWORD
from nucypher.blockchain.eth.constants import MIN_LOCKED_PERIODS, MAX_MINTING_PERIODS
from nucypher.blockchain.eth.registry import EthereumContractRegistry
from nucypher.characters.lawful import Ursula
from nucypher.cli.painting import BANNER, paint_configuration, paint_known_nodes, paint_contract_status
from nucypher.cli.protocol import UrsulaCommandProtocol
from nucypher.cli.types import (
    EIP55_CHECKSUM_ADDRESS,
    NETWORK_PORT,
    EXISTING_READABLE_FILE,
    EXISTING_WRITABLE_DIRECTORY,
    STAKE_VALUE,
    STAKE_DURATION
)
from nucypher.config.characters import UrsulaConfiguration
from nucypher.config.constants import DEFAULT_CONFIG_ROOT
from nucypher.utilities.logging import (
    logToSentry,
    getTextFileObserver,
    initialize_sentry,
    getJsonFileObserver,
    SimpleObserver)


FEDERATED_ONLY = False


#
# Click CLI Config
#

class NucypherClickConfig:

    __sentry_endpoint = "https://d8af7c4d692e4692a455328a280d845e@sentry.io/1310685"  # TODO: Use nucypher domain

    # Environment Variables
    config_file = os.environ.get('NUCYPHER_CONFIG_FILE', None)
    sentry_endpoint = os.environ.get("NUCYPHER_SENTRY_DSN", __sentry_endpoint)
    log_to_sentry = os.environ.get("NUCYPHER_SENTRY_LOGS", True)
    log_to_file = os.environ.get("NUCYPHER_FILE_LOGS", True)

    # Sentry Logging
    if log_to_sentry is True:
        initialize_sentry(dsn=__sentry_endpoint)
        globalLogPublisher.addObserver(logToSentry)

    # File Logging
    if log_to_file is True:
        globalLogPublisher.addObserver(getTextFileObserver())
        globalLogPublisher.addObserver(getJsonFileObserver())

    def __init__(self):
        self.log = Logger(self.__class__.__name__)
        self.__keyring_password = NO_PASSWORD

    def get_password(self, confirm: bool =False) -> str:
        keyring_password = os.environ.get("NUCYPHER_KEYRING_PASSWORD", NO_PASSWORD)

        if keyring_password is NO_PASSWORD:  # Collect password, prefer env var
            prompt = "Enter keyring password"
            keyring_password = click.prompt(prompt, confirmation_prompt=confirm, hide_input=True)

        self.__keyring_password = keyring_password
        return self.__keyring_password


# Register the above click configuration class as a decorator
nucypher_click_config = click.make_pass_decorator(NucypherClickConfig, ensure=True)


def echo_version(ctx, param, value):
    if not value or ctx.resilient_parsing:
        return
    click.secho(BANNER, bold=True)
    ctx.exit()


#
# Common CLI
#

@click.group()
@click.option('--version', help="Echo the CLI version", is_flag=True, callback=echo_version, expose_value=False, is_eager=True)
@click.option('-v', '--verbose', help="Specify verbosity level", count=True)
@nucypher_click_config
def nucypher_cli(click_config, verbose):
    click.echo(BANNER)
    click_config.verbose = verbose
    if click_config.verbose:
        click.secho("Verbose mode is enabled", fg='blue')


@nucypher_cli.command()
@click.option('--config-file', help="Path to configuration file", type=EXISTING_READABLE_FILE)
@nucypher_click_config
def status(click_config, config_file):
    """
    Echo a snapshot of live network metadata.
    """
    #
    # Initialize
    #
    ursula_config = UrsulaConfiguration.from_configuration_file(filepath=config_file)
    if not ursula_config.federated_only:
        ursula_config.connect_to_blockchain(provider_uri=ursula_config.provider_uri)
        ursula_config.connect_to_contracts()

        # Contracts
        paint_contract_status(ursula_config=ursula_config, click_config=click_config)

    # Known Nodes
    paint_known_nodes(ursula=ursula_config)


@nucypher_cli.command()
@click.argument('action')
@click.option('--debug', '-D', help="Enable debugging mode", is_flag=True)
@click.option('--dev', '-d', help="Enable development mode", is_flag=True)
@click.option('--quiet', '-Q', help="Disable logging", is_flag=True)
@click.option('--dry-run', '-x', help="Execute normally without actually starting the node", is_flag=True)
@click.option('--force', help="Don't ask for confirmation", is_flag=True)
@click.option('--teacher-uri', help="An Ursula URI to start learning from (seednode)", type=click.STRING)
@click.option('--min-stake', help="The minimum stake the teacher must have to be a teacher", type=click.INT, default=0)
@click.option('--rest-host', help="The host IP address to run Ursula network services on", type=click.STRING)
@click.option('--rest-port', help="The host port to run Ursula network services on", type=NETWORK_PORT)
@click.option('--db-filepath', help="The database filepath to connect to", type=click.STRING)
@click.option('--checksum-address', help="Run with a specified account", type=EIP55_CHECKSUM_ADDRESS)
@click.option('--federated-only', '-F', help="Connect only to federated nodes", is_flag=True, default=FEDERATED_ONLY)
@click.option('--poa', help="Inject POA middleware", is_flag=True)
@click.option('--config-root', help="Custom configuration directory", type=click.Path())
@click.option('--config-file', help="Path to configuration file", type=EXISTING_READABLE_FILE)
@click.option('--metadata-dir', help="Custom known metadata directory", type=EXISTING_WRITABLE_DIRECTORY)
@click.option('--provider-uri', help="Blockchain provider's URI", type=click.STRING)
@click.option('--no-registry', help="Skip importing the default contract registry", is_flag=True)
@click.option('--registry-filepath', help="Custom contract registry filepath", type=EXISTING_READABLE_FILE)
@nucypher_click_config
def ursula(click_config,
           action,
           debug,
           dev,
           quiet,
           dry_run,
           force,
           teacher_uri,
           min_stake,
           rest_host,
           rest_port,
           db_filepath,
           checksum_address,
           federated_only,
           poa,
           config_root,
           config_file,
           metadata_dir,  # TODO: Start nodes from an additional existing metadata dir
           provider_uri,
           no_registry,
           registry_filepath
           ) -> None:
    """
    Manage and run an Ursula node.

    \b
    Actions
    -------------------------------------------------
    \b
    run            Run an "Ursula" node.
    init           Create a new Ursula node configuration.
    view           View the Ursula node's configuration.
    forget         Forget all known nodes.
    save-metadata  Manually write node metadata to disk without running
    destroy        Delete Ursula node configuration.

    """

    #
    # Boring Setup Stuff
    #
    if not quiet:
        log = Logger('ursula.cli')

    if debug and quiet:
        raise click.BadOptionUsage(option_name="quiet", message="--debug and --quiet cannot be used at the same time.")

    if debug:
        click_config.log_to_sentry = False
        click_config.log_to_file = True
        globalLogPublisher.removeObserver(logToSentry)                          # Sentry
        globalLogPublisher.addObserver(SimpleObserver(log_level_name='debug'))  # Print

    elif quiet:
        globalLogPublisher.removeObserver(logToSentry)
        globalLogPublisher.removeObserver(SimpleObserver)
        globalLogPublisher.removeObserver(getJsonFileObserver())

    #
    # Launch Warnings
    #
    if not quiet:
        if dev:
            click.secho("WARNING: Running in development mode", fg='yellow')
        if federated_only:
            click.secho("WARNING: Running in Federated mode", fg='yellow')
        if force:
            click.secho("WARNING: Force is enabled", fg='yellow')

    #
    # Unauthenticated Configurations
    #
    if action == "init":
        """Create a brand-new persistent Ursula"""

        if dev and not quiet:
            click.secho("WARNING: Using temporary storage area", fg='yellow')

        if not config_root:                         # Flag
            config_root = click_config.config_file  # Envvar

        if not rest_host:
            rest_host = click.prompt("Enter Ursula's public-facing IPv4 address")

        ursula_config = UrsulaConfiguration.generate(password=click_config.get_password(confirm=True),
                                                     config_root=config_root,
                                                     rest_host=rest_host,
                                                     rest_port=rest_port,
                                                     db_filepath=db_filepath,
                                                     federated_only=federated_only,
                                                     checksum_public_address=checksum_address,
                                                     no_registry=federated_only or no_registry,
                                                     registry_filepath=registry_filepath,
                                                     provider_uri=provider_uri,
                                                     poa=poa)

        if not quiet:
            click.secho("Generated keyring {}".format(ursula_config.keyring_dir), fg='green')
            click.secho("Saved configuration file {}".format(ursula_config.config_file_location), fg='green')

            # Give the use a suggestion as to what to do next...
            how_to_run_message = "\nTo run an Ursula node from the default configuration filepath run: \n\n'{}'\n"
            suggested_command = 'nucypher ursula run'
            if config_root is not None:
                config_file_location = os.path.join(config_root, config_file or UrsulaConfiguration.CONFIG_FILENAME)
                suggested_command += ' --config-file {}'.format(config_file_location)
            click.secho(how_to_run_message.format(suggested_command), fg='green')
            return  # FIN

        else:
            click.secho("OK")

    elif action == "destroy":
        """Delete all configuration files from the disk"""

        if dev:
            message = "'nucypher ursula destroy' cannot be used in --dev mode"
            raise click.BadOptionUsage(option_name='--dev', message=message)

        try:
            ursula_config = UrsulaConfiguration.from_configuration_file(filepath=config_file)

        except FileNotFoundError:
            config_root = config_root or DEFAULT_CONFIG_ROOT
            config_file_location = config_file or UrsulaConfiguration.DEFAULT_CONFIG_FILE_LOCATION

            if not force:
                message = "No configuration file found at {}; \n" \
                          "Destroy top-level configuration directory: {}?".format(config_file_location, config_root)
                click.confirm(message, abort=True)  # ABORT

            shutil.rmtree(config_root, ignore_errors=False)

        else:
            if not force:
                click.confirm('''
*Permanently and irreversibly delete all* nucypher files including
    - Private and Public Keys
    - Known Nodes
    - TLS certificates
    - Node Configurations
    - Log Files

Delete {}?'''.format(ursula_config.config_root), abort=True)

            try:
                ursula_config.destroy(force=force)
            except FileNotFoundError:
                message = 'Failed: No nucypher files found at {}'.format(ursula_config.config_root)
                click.secho(message, fg='red')
                log.debug(message)
                raise click.Abort()
            else:
                message = "Deleted configuration files at {}".format(ursula_config.config_root)
                click.secho(message, fg='green')
                log.debug(message)

        if not quiet:
            click.secho("Destroyed {}".format(config_root))

        return

    # Development Configuration
    if dev:
        ursula_config = UrsulaConfiguration(dev_mode=True,
                                            poa=poa,
                                            registry_filepath=registry_filepath,
                                            provider_uri=provider_uri,
                                            checksum_public_address=checksum_address,
                                            federated_only=federated_only,
                                            rest_host=rest_host,
                                            rest_port=rest_port,
                                            db_filepath=db_filepath)
    # Authenticated Configurations
    else:

        # Restore configuration from file
        ursula_config = UrsulaConfiguration.from_configuration_file(filepath=config_file
                                                                    # TODO: CLI Overrides for file-based configurations
                                                                    # poa = poa,
                                                                    # registry_filepath = registry_filepath,
                                                                    # provider_uri = provider_uri,
                                                                    # checksum_public_address = checksum_public_address,
                                                                    # federated_only = federated_only,
                                                                    # rest_host = rest_host,
                                                                    # rest_port = rest_port,
                                                                    # db_filepath = db_filepath
                                                                    )

        try:  # Unlock Keyring
            # ursula_config.attach_keyring()
            if not quiet:
                click.secho('Decrypting keyring...', fg='blue')
            ursula_config.keyring.unlock(password=click_config.get_password())  # Takes ~3 seconds, ~1GB Ram
        except CryptoError:
            raise ursula_config.keyring.AuthenticationFailed

    if not ursula_config.federated_only:
        try:
            ursula_config.connect_to_blockchain(recompile_contracts=False)
            ursula_config.connect_to_contracts()
        except EthereumContractRegistry.NoRegistry:
            message = "Cannot configure blockchain character: No contract registry found;  Did you mean to pass --federated-only?"
            raise EthereumContractRegistry.NoRegistry(message)

    click_config.ursula_config = ursula_config  # Pass Ursula's config onto staking sub-command

    #
    # Action Switch
    #
    if action == 'run':
        """Seed, Produce, Run!"""

        #
        # Seed - Step 1
        #
        teacher_nodes = list()
        if teacher_uri:
            node = Ursula.from_teacher_uri(teacher_uri=teacher_uri, min_stake=min_stake, federated_only=federated_only)
            teacher_nodes.append(node)

        #
        # Produce - Step 2
        #
        ursula = ursula_config(known_nodes=teacher_nodes)
        ursula_config.log.debug("Initialized Ursula {}".format(ursula), fg='green')

        # GO!
        try:

            #
            # Run - Step 3
            #
            click.secho("Running Ursula on {}".format(ursula.rest_interface), fg='green', bold=True)
            if not debug:
                stdio.StandardIO(UrsulaCommandProtocol(ursula=ursula))

            if dry_run:
                # That's all folks!
                return

            ursula.get_deployer().run()  # <--- Blocking Call (Reactor)

        except Exception as e:
            ursula_config.log.critical(str(e))
            click.secho("{} {}".format(e.__class__.__name__, str(e)), fg='red')
            raise  # Crash :-(

        finally:
            if not quiet:
                click.secho("Stopping Ursula")
            ursula_config.cleanup()
            if not quiet:
                click.secho("Ursula Stopped", fg='red')

        return

    elif action == "save-metadata":
        """Manually save a node self-metadata file"""

        ursula = ursula_config.produce(ursula_config=ursula_config)
        metadata_path = ursula.write_node_metadata(node=ursula)
        if not quiet:
            click.secho("Successfully saved node metadata to {}.".format(metadata_path), fg='green')
        return

    elif action == "view":
        """Paint an existing configuration to the console"""
        paint_configuration(config_filepath=config_file or ursula_config.config_file_location)
        return

    elif action == "forget":
        """Forget all known nodes via storages"""
        click.confirm("Permanently delete all known node data?", abort=True)
        ursula_config.forget_nodes()
        message = "Removed all stored node node metadata and certificates"
        click.secho(message=message, fg='red')
        return

    else:
        raise click.BadArgumentUsage("No such argument {}".format(action))


@click.argument('action', default='list', required=False)
@click.option('--checksum-address', type=EIP55_CHECKSUM_ADDRESS)
@click.option('--value', help="Token value of stake", type=STAKE_VALUE)
@click.option('--duration', help="Period duration of stake", type=STAKE_DURATION)
@click.option('--index', help="A specific stake index to resume", type=click.INT)
@nucypher_click_config
def stake(click_config,
          action,
          checksum_address,
          index,
          value,
          duration):
    """
    Manage token staking.  TODO

    \b
    Actions
    -------------------------------------------------
    \b
    list              List all stakes for this node.
    init              Stage a new stake.
    confirm-activity  Manually confirm-activity for the current period.
    divide            Divide an existing stake.
    collect-reward    Withdraw staking reward.

    """
    ursula_config = click_config.ursula_config

    #
    # Initialize
    #
    if not ursula_config.federated_only:
        ursula_config.connect_to_blockchain(click_config)
        ursula_config.connect_to_contracts(click_config)

    if not checksum_address:

        if click_config.accounts == NO_BLOCKCHAIN_CONNECTION:
            click.echo('No account found.')
            raise click.Abort()

        for index, address in enumerate(click_config.accounts):
            if index == 0:
                row = 'etherbase (0) | {}'.format(address)
            else:
                row = '{} .......... | {}'.format(index, address)
            click.echo(row)

        click.echo("Select ethereum address")
        account_selection = click.prompt("Enter 0-{}".format(len(ur.accounts)), type=click.INT)
        address = click_config.accounts[account_selection]

    if action == 'list':
        live_stakes = ursula_config.miner_agent.get_all_stakes(miner_address=checksum_address)
        for index, stake_info in enumerate(live_stakes):
            row = '{} | {}'.format(index, stake_info)
            click.echo(row)

    elif action == 'init':
        click.confirm("Stage a new stake?", abort=True)

        live_stakes = ursula_config.miner_agent.get_all_stakes(miner_address=checksum_address)
        if len(live_stakes) > 0:
            raise RuntimeError("There is an existing stake for {}".format(checksum_address))

        # Value
        balance = ursula_config.miner_agent.token_agent.get_balance(address=checksum_address)
        click.echo("Current balance: {}".format(balance))
        value = click.prompt("Enter stake value", type=click.INT)

        # Duration
        message = "Minimum duration: {} | Maximum Duration: {}".format(MIN_LOCKED_PERIODS, MAX_MINTING_PERIODS)
        click.echo(message)
        duration = click.prompt("Enter stake duration in periods (1 Period = 24 Hours)", type=click.INT)

        start_period = ursula_config.miner_agent.get_current_period()
        end_period = start_period + duration

        # Review
        click.echo("""

        | Staged Stake |

        Node: {address}
        Value: {value}
        Duration: {duration}
        Start Period: {start_period}
        End Period: {end_period}

        """.format(address=checksum_address,
                   value=value,
                   duration=duration,
                   start_period=start_period,
                   end_period=end_period))

        raise NotImplementedError

    elif action == 'confirm-activity':
        """Manually confirm activity for the active period"""
        stakes = ursula_config.miner_agent.get_all_stakes(miner_address=checksum_address)
        if len(stakes) == 0:
            raise RuntimeError("There are no active stakes for {}".format(checksum_address))
        ursula_config.miner_agent.confirm_activity(node_address=checksum_address)

    elif action == 'divide':
        """Divide an existing stake by specifying the new target value and end period"""

        stakes = ursula_config.miner_agent.get_all_stakes(miner_address=checksum_address)
        if len(stakes) == 0:
            raise RuntimeError("There are no active stakes for {}".format(checksum_address))

        if not index:
            for selection_index, stake_info in enumerate(stakes):
                click.echo("{} ....... {}".format(selection_index, stake_info))
            index = click.prompt("Select a stake to divide", type=click.INT)

        target_value = click.prompt("Enter new target value", type=click.INT)
        extension = click.prompt("Enter number of periods to extend", type=click.INT)

        click.echo("""
        Current Stake: {}

        New target value {}
        New end period: {}

        """.format(stakes[index],
                   target_value,
                   target_value + extension))

        click.confirm("Is this correct?", abort=True)
        ursula_config.miner_agent.divide_stake(miner_address=checksum_address,
                                               stake_index=index,
                                               value=value,
                                               periods=extension)

    elif action == 'collect-reward':          # TODO: Implement
        """Withdraw staking reward to the specified wallet address"""
        # click.confirm("Send {} to {}?".format)
        # ursula_config.miner_agent.collect_staking_reward(collector_address=address)
        raise NotImplementedError

    else:
        raise click.BadArgumentUsage("No such argument {}".format(action))
