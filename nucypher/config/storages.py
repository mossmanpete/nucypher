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


import binascii
import glob
import os
import tempfile
from abc import abstractmethod, ABC

import OpenSSL
import boto3 as boto3
import shutil
from botocore.errorfactory import ClientError
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509 import Certificate
from twisted.logger import Logger
from typing import Callable, Tuple, Union, Set, Any

from constant_sorrow.constants import NO_STORAGE_AVAILIBLE
from nucypher.config.constants import DEFAULT_CONFIG_ROOT
from nucypher.utilities.decorators import validate_checksum_address


class NodeStorage(ABC):

    _name = NotImplemented
    _TYPE_LABEL = 'storage_type'
    NODE_SERIALIZER = binascii.hexlify
    NODE_DESERIALIZER = binascii.unhexlify
    TLS_CERTIFICATE_ENCODING = Encoding.PEM
    TLS_CERTIFICATE_EXTENSION = '.{}'.format(TLS_CERTIFICATE_ENCODING.name.lower())

    class NodeStorageError(Exception):
        pass

    class UnknownNode(NodeStorageError):
        pass

    def __init__(self,
                 federated_only: bool,  # TODO# 466
                 character_class=None,
                 serializer: Callable = NODE_SERIALIZER,
                 deserializer: Callable = NODE_DESERIALIZER,
                 ) -> None:

        from nucypher.characters.lawful import Ursula

        self.log = Logger(self.__class__.__name__)
        self.serializer = serializer
        self.deserializer = deserializer
        self.federated_only = federated_only
        self.character_class = character_class or Ursula

    def __getitem__(self, item):
        return self.get(checksum_address=item, federated_only=self.federated_only)

    def __setitem__(self, key, value):
        return self.store_node_metadata(node=value)

    def __delitem__(self, key):
        self.remove(checksum_address=key)

    def __iter__(self):
        return self.all(federated_only=self.federated_only)

    def _read_common_name(self, certificate: Certificate):
        x509 = OpenSSL.crypto.X509.from_cryptography(certificate)
        subject_components = x509.get_subject().get_components()
        common_name_as_bytes = subject_components[0][1]
        common_name_from_cert = common_name_as_bytes.decode()
        return common_name_from_cert

    @abstractmethod
    def store_node_certificate(self,
                               host: str,
                               checksum_address: str,
                               certificate: Certificate,
                               force: bool = False
                               ) -> str:
        raise NotImplementedError

    @abstractmethod
    def store_node_metadata(self, node):
        """Save a single node's metadata and tls certificate"""
        raise NotImplementedError

    @abstractmethod
    def generate_certificate_filepath(self, checksum_address: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def payload(self) -> dict:
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def from_payload(self, data: dict, *args, **kwargs) -> 'NodeStorage':
        """Instantiate a storage object from a dictionary"""
        raise NotImplementedError

    @abstractmethod
    def initialize(self):
        """One-time initialization steps to establish a node storage backend"""
        raise NotImplementedError

    @abstractmethod
    def all(self, federated_only: bool, certificates_only: bool = False) -> set:
        """Return s set of all stored nodes"""
        raise NotImplementedError

    @abstractmethod
    def get(self, checksum_address: str, federated_only: bool):
        """Retrieve a single stored node"""
        raise NotImplementedError

    @abstractmethod
    def remove(self, checksum_address: str) -> bool:
        """Remove a single stored node"""
        raise NotImplementedError

    @abstractmethod
    def clear(self) -> bool:
        """Remove all stored nodes"""
        raise NotImplementedError


class ForgetfulNodeStorage(NodeStorage):

    _name = ':memory:'
    __base_prefix = 'nucypher-temp-cert-'

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.__metadata = dict()
        self.__certificates = dict()

        self.__rollover_certificates = list()

    def all(self, federated_only: bool, certificates_only: bool = False) -> set:
        return set(self.__metadata.values() if not certificates_only else self.__certificates.values())

    @validate_checksum_address
    def get(self,
            federated_only: bool,
            host: str = None,
            checksum_address: str = None,
            certificate_only: bool = False):

        if not bool(checksum_address) ^ bool(host):
            message = "Either pass checksum_address or host; Not both. Got ({} {})".format(checksum_address, host)
            raise ValueError(message)

        if certificate_only is True:
            try:
                return self.__certificates[checksum_address or host]
            except KeyError:
                raise self.UnknownNode
        else:
            try:
                return self.__metadata[checksum_address or host]
            except KeyError:
                raise self.UnknownNode

    def forget(self, everything: bool = True) -> bool:
        for temp_certificate in self.__rollover_certificates:
            os.remove(temp_certificate)

        if everything is True:
            pattern = '/tmp/{}*'.format(self.__base_prefix)
            for temp_certificate in glob.glob(pattern):
                os.remove(temp_certificate)
                return len(glob.glob(pattern)) == 0

        return len(self.__rollover_certificates) == 0

    def store_host_certificate(self, host: str, certificate: Certificate):
        self.__certificates[host] = certificate
        return self.generate_certificate_filepath(host=host)

    @validate_checksum_address
    def store_node_certificate(self,
                               certificate: Certificate,
                               checksum_address: str,
                               host: str = None,
                               force: bool = False
                               ) -> str:

        self.__certificates[checksum_address] = certificate
        return self.generate_certificate_filepath(checksum_address=checksum_address)

    def store_node_metadata(self, node):
        self.__metadata[node.checksum_public_address] = node
        return self.__metadata[node.checksum_public_address]

    @validate_checksum_address
    def generate_certificate_filepath(self,
                                      checksum_address: str = None,
                                      host: str = None) -> str:

        if not bool(checksum_address) ^ bool(host):
            message = "Either pass checksum_address or host; Not both. Got ({} {})".format(checksum_address, host)
            raise ValueError(message)

        prefix = '{}{}-'.format(self.__base_prefix, checksum_address or host)
        temp_file = tempfile.NamedTemporaryFile(prefix=prefix, suffix=self.TLS_CERTIFICATE_EXTENSION, delete=False)
        certificate = self.__certificates[checksum_address or host]
        certificate_bytes = certificate.public_bytes(self.TLS_CERTIFICATE_ENCODING)
        temp_file.write(certificate_bytes)

        self.__rollover_certificates.append(temp_file.name)
        return temp_file.name

    @validate_checksum_address
    def remove(self,
               checksum_address: str,
               metadata: bool = True,
               certificate: bool = True
               ) -> Tuple[bool, str]:

        if metadata is True:
            del self.__metadata[checksum_address]
        if certificate is True:
            del self.__certificates[checksum_address]
        return True, checksum_address

    def clear(self, metadata: bool = True, certificates: bool = True) -> None:
        """Forget all stored nodes and certificates"""
        if metadata is True:
            self.__metadata = dict()
        if certificates is True:
            self.__certificates = dict()

    def payload(self) -> dict:
        payload = {self._TYPE_LABEL: self._name}
        return payload

    @classmethod
    def from_payload(cls, payload: dict, *args, **kwargs) -> 'ForgetfulNodeStorage':
        """Alternate constructor to create a storage instance from JSON-like configuration"""
        if payload[cls._TYPE_LABEL] != cls._name:
            raise cls.NodeStorageError
        return cls(*args, **kwargs)

    def initialize(self) -> bool:
        """Returns True if initialization was successful"""
        self.__metadata = dict()
        self.__certificates = dict()
        return not bool(self.__metadata or self.__certificates)


class LocalFileBasedNodeStorage(NodeStorage):

    _name = 'local'
    __METADATA_FILENAME_TEMPLATE = '{}.node'

    class NoNodeMetadataFileFound(FileNotFoundError, NodeStorage.UnknownNode):
        pass

    def __init__(self,
                 config_root: str = None,
                 storage_root: str = None,
                 metadata_dir: str = None,
                 certificates_dir: str = None,
                 *args, **kwargs
                 ) -> None:

        super().__init__(*args, **kwargs)
        self.log = Logger(self.__class__.__name__)

        self.root_dir = storage_root
        self.metadata_dir = metadata_dir
        self.certificates_dir = certificates_dir
        self._cache_storage_filepaths(config_root=config_root)

    @staticmethod
    def _generate_storage_filepaths(config_root: str = None,
                                    storage_root: str = None,
                                    metadata_dir: str = None,
                                    certificates_dir: str = None):

        storage_root = storage_root or os.path.join(config_root or DEFAULT_CONFIG_ROOT, 'known_nodes')
        metadata_dir = metadata_dir or os.path.join(storage_root, 'metadata')
        certificates_dir = certificates_dir or os.path.join(storage_root, 'certificates')

        payload = {'storage_root': storage_root,
                   'metadata_dir': metadata_dir,
                   'certificates_dir': certificates_dir}

        return payload

    def _cache_storage_filepaths(self, config_root: str = None):
        filepaths = self._generate_storage_filepaths(config_root=config_root,
                                                     storage_root=self.root_dir,
                                                     metadata_dir=self.metadata_dir,
                                                     certificates_dir=self.certificates_dir)
        self.root_dir = filepaths['storage_root']
        self.metadata_dir = filepaths['metadata_dir']
        self.certificates_dir = filepaths['certificates_dir']

    #
    # Certificates
    #

    @validate_checksum_address
    def __get_certificate_filename(self, checksum_address: str):
        return '{}.{}'.format(checksum_address, Encoding.PEM.name.lower())

    def __get_certificate_filepath(self, certificate_filename: str) -> str:
        return os.path.join(self.certificates_dir, certificate_filename)

    @validate_checksum_address
    def generate_certificate_filepath(self, checksum_address: str) -> str:
        certificate_filename = self.__get_certificate_filename(checksum_address)
        certificate_filepath = self.__get_certificate_filepath(certificate_filename=certificate_filename)
        return certificate_filepath

    @validate_checksum_address
    def __write_tls_certificate(self,
                                checksum_address: str,
                                certificate: Certificate,
                                host: str = None,
                                force: bool = False) -> str:

        # Read
        x509 = OpenSSL.crypto.X509.from_cryptography(certificate)
        subject_components = x509.get_subject().get_components()
        common_name_as_bytes = subject_components[0][1]
        common_name_on_certificate = common_name_as_bytes.decode()
        if not host:
            host = common_name_on_certificate

        # Validate
        # TODO: It's better for us to have checked this a while ago so that this situation is impossible.  #443
        if host and (host != common_name_on_certificate):
            raise ValueError('You passed a hostname ("{}") that does not match the certificat\'s common name.'.format(host))

        certificate_filepath = self.generate_certificate_filepath(checksum_address=checksum_address)
        certificate_already_exists = os.path.isfile(certificate_filepath)
        if force is False and certificate_already_exists:
            raise FileExistsError('A TLS certificate already exists at {}.'.format(certificate_filepath))

        # Write
        with open(certificate_filepath, 'wb') as certificate_file:
            public_pem_bytes = certificate.public_bytes(self.TLS_CERTIFICATE_ENCODING)
            certificate_file.write(public_pem_bytes)

        self.certificate_filepath = certificate_filepath
        self.log.info("Saved TLS certificate for {}: {}".format(self, certificate_filepath))

        return certificate_filepath

    @validate_checksum_address
    def __read_tls_public_certificate(self, filepath: str = None, checksum_address: str=None) -> Certificate:
        """Deserialize an X509 certificate from a filepath"""
        if not bool(filepath) ^ bool(checksum_address):
            raise ValueError("Either pass filepath or checksum_address; Not both.")

        if not filepath and checksum_address is not None:
            filepath = self.generate_certificate_filepath(checksum_address)

        try:
            with open(filepath, 'rb') as certificate_file:
                cert = x509.load_pem_x509_certificate(certificate_file.read(), backend=default_backend())
                return cert
        except FileNotFoundError:
            raise FileNotFoundError("No SSL certificate found at {}".format(filepath))

    #
    # Metadata
    #

    @validate_checksum_address
    def __generate_metadata_filepath(self, checksum_address: str) -> str:
        metadata_path = os.path.join(self.metadata_dir, self.__METADATA_FILENAME_TEMPLATE.format(checksum_address))
        return metadata_path

    def __read_metadata(self, filepath: str, federated_only: bool):
        from nucypher.characters.lawful import Ursula
        try:
            with open(filepath, "rb") as seed_file:
                seed_file.seek(0)
                node_bytes = self.deserializer(seed_file.read())
                node = Ursula.from_bytes(node_bytes, federated_only=federated_only)
        except FileNotFoundError:
            raise self.UnknownNode
        return node

    def __write_metadata(self, filepath: str, node):
        with open(filepath, "wb") as f:
            f.write(self.serializer(self.character_class.__bytes__(node)))
        self.log.info("Wrote new node metadata to filesystem {}".format(filepath))
        return filepath

    #
    # API
    #
    def all(self, federated_only: bool, certificates_only: bool = False) -> Set[Union[Any, Certificate]]:
        filenames = os.listdir(self.certificates_dir if certificates_only else self.metadata_dir)
        self.log.info("Found {} known node metadata files at {}".format(len(filenames), self.metadata_dir))

        known_certificates = set()
        if certificates_only:
            for filename in filenames:
                certificate = self.__read_tls_public_certificate(os.path.join(self.certificates_dir, filename))
                known_certificates.add(certificate)
            return known_certificates

        else:
            known_nodes = set()
            for filename in filenames:
                metadata_path = os.path.join(self.metadata_dir, filename)
                node = self.__read_metadata(filepath=metadata_path, federated_only=federated_only)   # TODO: 466
                known_nodes.add(node)
            return known_nodes

    @validate_checksum_address
    def get(self, checksum_address: str, federated_only: bool, certificate_only: bool = False):
        if certificate_only is True:
            certificate = self.__read_tls_public_certificate(checksum_address=checksum_address)
            return certificate
        metadata_path = self.__generate_metadata_filepath(checksum_address=checksum_address)
        node = self.__read_metadata(filepath=metadata_path, federated_only=federated_only)   # TODO: 466
        return node

    @validate_checksum_address
    def store_node_certificate(self,
                               checksum_address: str,
                               certificate: Certificate,
                               host: str = None,
                               force: bool = True
                               ) -> str:

        certificate_filepath = self.__write_tls_certificate(certificate=certificate,
                                                            checksum_address=checksum_address,
                                                            host=host,
                                                            force=force)

        return certificate_filepath

    def store_node_metadata(self, node) -> str:
        filepath = self.__generate_metadata_filepath(checksum_address=node.checksum_public_address)
        self.__write_metadata(filepath=filepath, node=node)
        return filepath

    def save_node(self, node, force) -> Tuple[str, str]:
        certificate_filepath = self.store_node_certificate(checksum_address=node.checksum_public_address,
                                                           certificate=node.certificate,
                                                           force=force)
        metadata_filepath = self.store_node_metadata(node=node)
        return metadata_filepath, certificate_filepath

    @validate_checksum_address
    def remove(self, checksum_address: str, metadata: bool = True, certificate: bool = True) -> None:

        if metadata is True:
            metadata_filepath = self.__generate_metadata_filepath(checksum_address=checksum_address)
            os.remove(metadata_filepath)
            self.log.debug("Deleted {} from the filesystem".format(checksum_address))

        if certificate is True:
            certificate_filepath = self.generate_certificate_filepath(checksum_address=checksum_address)
            os.remove(certificate_filepath)
            self.log.debug("Deleted {} from the filesystem".format(checksum_address))

        return

    def clear(self, metadata: bool = True, certificates: bool = True) -> None:
        """Forget all stored nodes and certificates"""

        def __destroy_dir_contents(path):
            for file in os.listdir(path):
                file_path = os.path.join(path, file)
                if os.path.isfile(file_path):
                    os.unlink(file_path)

        if metadata is True:
            __destroy_dir_contents(self.metadata_dir)
        if certificates is True:
            __destroy_dir_contents(self.certificates_dir)

        return

    def payload(self) -> dict:
        payload = {
            'storage_type': self._name,
            'storage_root': self.root_dir,
            'metadata_dir': self.metadata_dir,
            'certificates_dir': self.certificates_dir
        }
        return payload

    @classmethod
    def from_payload(cls, payload: dict, *args, **kwargs) -> 'LocalFileBasedNodeStorage':
        storage_type = payload[cls._TYPE_LABEL]
        if not storage_type == cls._name:
            raise cls.NodeStorageError("Wrong storage type. got {}".format(storage_type))
        del payload['storage_type']

        return cls(*args, **payload, **kwargs)

    def initialize(self) -> bool:
        try:
            os.mkdir(self.root_dir, mode=0o755)
            os.mkdir(self.metadata_dir, mode=0o755)
            os.mkdir(self.certificates_dir, mode=0o755)
        except FileExistsError:
            message = "There are pre-existing files at {}".format(self.root_dir)
            raise self.NodeStorageError(message)
        except FileNotFoundError:
            raise self.NodeStorageError("There is no existing configuration at {}".format(self.root_dir))

        return bool(all(map(os.path.isdir, (self.root_dir, self.metadata_dir, self.certificates_dir))))


class TemporaryFileBasedNodeStorage(LocalFileBasedNodeStorage):
    _name = 'tmp'

    def __init__(self, *args, **kwargs):
        self.__temp_metadata_dir = None
        self.__temp_certificates_dir = None
        super().__init__(metadata_dir=self.__temp_metadata_dir,
                         certificates_dir=self.__temp_certificates_dir,
                         *args, **kwargs)

    def __del__(self):
        if self.__temp_metadata_dir is not None:
            shutil.rmtree(self.__temp_metadata_dir, ignore_errors=True)
            shutil.rmtree(self.__temp_certificates_dir, ignore_errors=True)

    def initialize(self) -> bool:

        # Metadata
        self.__temp_metadata_dir = tempfile.mkdtemp(prefix="nucypher-tmp-nodes-")
        self.metadata_dir = self.__temp_metadata_dir

        # Certificates
        self.__temp_certificates_dir = tempfile.mkdtemp(prefix="nucypher-tmp-certs-")
        self.certificates_dir = self.__temp_certificates_dir

        return bool(os.path.isdir(self.metadata_dir) and os.path.isdir(self.certificates_dir))


class S3NodeStorage(NodeStorage):

    _name = 's3'
    S3_ACL = 'private'  # Canned S3 Permissions

    def __init__(self,
                 bucket_name: str,
                 s3_resource=None,
                 *args, **kwargs) -> None:

        super().__init__(*args, **kwargs)
        self.__bucket_name = bucket_name
        self.__s3client = boto3.client('s3')
        self.__s3resource = s3_resource or boto3.resource('s3')
        self.__bucket = NO_STORAGE_AVAILIBLE

    @property
    def bucket(self):
        return self.__bucket

    @property
    def bucket_name(self):
        return self.__bucket_name

    def __read(self, node_obj: str):
        try:
            node_object_metadata = node_obj.get()
        except ClientError:
            raise self.UnknownNode
        node_bytes = self.deserializer(node_object_metadata['Body'].read())
        node = self.character_class.from_bytes(node_bytes)
        return node

    @validate_checksum_address
    def generate_presigned_url(self, checksum_address: str) -> str:
        payload = {'Bucket': self.__bucket_name, 'Key': checksum_address}
        url = self.__s3client.generate_presigned_url('get_object', payload, ExpiresIn=900)
        return url

    def all(self, federated_only: bool, certificates_only: bool = False) -> set:
        node_objs = self.__bucket.objects.all()
        nodes = set()
        for node_obj in node_objs:
            node = self.__read(node_obj=node_obj)
            nodes.add(node)
        return nodes

    @validate_checksum_address
    def get(self, checksum_address: str, federated_only: bool):
        node_obj = self.__bucket.Object(checksum_address)
        node = self.__read(node_obj=node_obj)
        return node

    def store_node_metadata(self, node):
        self.__s3client.put_object(Bucket=self.__bucket_name,
                                   ACL=self.S3_ACL,
                                   Key=node.checksum_public_address,
                                   Body=self.serializer(bytes(node)))

    @validate_checksum_address
    def remove(self, checksum_address: str) -> bool:
        node_obj = self.__bucket.Object(checksum_address)
        response = node_obj.delete()
        if response['ResponseMetadata']['HTTPStatusCode'] != 204:
            raise self.NodeStorageError("S3 Storage failed to delete node {}".format(checksum_address))
        return True

    def payload(self) -> dict:
        payload = {
            self._TYPE_LABEL: self._name,
            'bucket_name': self.__bucket_name
        }
        return payload

    @classmethod
    def from_payload(cls, payload: dict, *args, **kwargs):
        return cls(bucket_name=payload['bucket_name'], *args, **kwargs)

    def initialize(self):
        self.__bucket = self.__s3resource.Bucket(self.__bucket_name)


### Node Storage Registry ###
NODE_STORAGES = {storage_class._name: storage_class for storage_class in NodeStorage.__subclasses__()}
