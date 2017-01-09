# -*- coding: utf-8 -*-

import datetime
import os
import shutil
import tempfile

import boto3

import freezegun

from moto import mock_s3

import s3backup
from s3backup.clients.local import LocalSyncClient
from s3backup.clients.s3 import S3SyncClient


def get_pairs(list_of_things):
    for i in range(len(list_of_things)):
        yield list_of_things[i], list_of_things[(i + 1) % len(list_of_things)]


class TestGetPairs(object):
    def test_empty(self):
        assert list(get_pairs([])) == []

    def test_correct_output(self):
        assert list(get_pairs(['a', 'b', 'c'])) == [('a', 'b'), ('b', 'c'), ('c', 'a')]


def set_local_contents(folder, key, timestamp=None, data=''):
    path = os.path.join(folder, key)
    parent = os.path.dirname(path)
    if not os.path.exists(parent):
        os.makedirs(parent)
    with open(path, 'w') as fp:
        fp.write(data)
    if timestamp is not None:
        os.utime(path, (timestamp, timestamp))


def set_s3_contents(client, bucket, key, timestamp=None, data=''):
    if timestamp is None:
        freeze_time = datetime.datetime.utcnow()
    else:
        freeze_time = datetime.datetime.utcfromtimestamp(timestamp)

    with freezegun.freeze_time(freeze_time):
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
        )


class TestGetActions(object):
    @mock_s3
    def test_empty_clients(self):
        s3_client = boto3.client(
            's3',
            aws_access_key_id='',
            aws_secret_access_key='',
            aws_session_token='',
        )
        s3_client.create_bucket(Bucket='testbucket')

        client_1 = LocalSyncClient(tempfile.mkdtemp())
        client_2 = S3SyncClient(s3_client, 'testbucket', 'foo')
        actual_output = list(s3backup.get_actions(client_1, client_2))
        assert actual_output == []


class TestIntegrations(object):
    def setup_method(self):
        self.clients = []
        self.folders = []

    def teardown_method(self):
        for folder in self.folders:
            shutil.rmtree(folder)

    def create_local_client(self):
        folder = tempfile.mkdtemp()
        client = LocalSyncClient(folder)
        self.folders.append(folder)
        self.clients.append(client)
        return client, folder

    def create_s3_client(self, s3_client, bucket, prefix):
        client = S3SyncClient(s3_client, bucket, prefix)
        self.clients.append(client)
        return client

    @mock_s3
    def test_local_with_s3(self):
        s3_client = boto3.client(
            's3',
            aws_access_key_id='',
            aws_secret_access_key='',
            aws_session_token='',
        )
        s3_client.create_bucket(Bucket='testbucket')
        set_s3_contents(s3_client, 'testbucket', 'asgard/colors/cream', 9999, '#ddeeff')

        _, target_folder = self.create_local_client()
        set_local_contents(target_folder, 'colors/red', 5000, '#ff0000')
        set_local_contents(target_folder, 'colors/green', 3000, '#00ff00')
        set_local_contents(target_folder, 'colors/blue', 2000, '#0000ff')

        self.create_s3_client(s3_client, 'testbucket', 'asgard')

        self.sync_clients()

        expected_keys = [
            'colors/red',
            'colors/green',
            'colors/blue',
            'colors/cream'
        ]
        self.assert_local_keys(expected_keys)
        self.assert_contents('colors/red', b'#ff0000')
        self.assert_contents('colors/green', b'#00ff00')
        self.assert_contents('colors/blue', b'#0000ff')
        self.assert_contents('colors/cream', b'#ddeeff')
        self.assert_remote_timestamp('colors/red', 5000)
        self.assert_remote_timestamp('colors/green', 3000)
        self.assert_remote_timestamp('colors/blue', 2000)
        self.assert_remote_timestamp('colors/cream', 9999)

        self.delete_local(target_folder, 'colors/red')

        self.sync_clients()
        expected_keys = [
            'colors/green',
            'colors/blue',
            'colors/cream'
        ]
        self.assert_local_keys(expected_keys)

    def sync_clients(self):
        for client_1, client_2 in get_pairs(self.clients):
            s3backup.sync(client_1, client_2)

    def delete_local(self, folder, key):
        os.remove(os.path.join(folder, key))

    def assert_file_existence(self, keys, exists):
        for folder in self.folders:
            for key in keys:
                assert os.path.exists(os.path.join(folder, key)) is exists

    def assert_contents(self, key, data=None, timestamp=None):
        for client in self.clients:
            sync_object = client.get(key)
            if data is not None:
                assert sync_object.fp.read() == data
            if timestamp is not None:
                assert sync_object.timestamp == timestamp

    def assert_remote_timestamp(self, key, expected_timestamp):
        for client in self.clients:
            assert client.get_remote_timestamp(key) == expected_timestamp

    def assert_local_keys(self, expected_keys):
        for client in self.clients:
            assert sorted(client.get_local_keys()) == sorted(expected_keys)

    def test_fresh_sync(self):
        self.create_local_client()
        self.create_local_client()

        set_local_contents(self.folders[0], 'foo', timestamp=1000)
        set_local_contents(self.folders[0], 'bar', timestamp=2000)
        set_local_contents(self.folders[1], 'baz', timestamp=3000, data='what is up?')

        self.sync_clients()

        self.assert_local_keys(['foo', 'bar', 'baz'])
        self.assert_remote_timestamp('foo', 1000)
        self.assert_remote_timestamp('bar', 2000)
        self.assert_remote_timestamp('baz', 3000)
        self.assert_file_existence(['foo', 'bar', 'baz'], True)
        self.assert_contents('baz', b'what is up?')

        self.delete_local(self.folders[0], 'foo')
        set_local_contents(self.folders[0], 'test', timestamp=5000)
        set_local_contents(self.folders[1], 'hello', timestamp=6000)
        set_local_contents(self.folders[1], 'baz', timestamp=8000, data='just syncing some stuff')

        self.sync_clients()

        self.assert_file_existence(['foo'], False)
        self.assert_local_keys(['test', 'bar', 'baz', 'hello'])
        self.assert_remote_timestamp('bar', 2000)
        self.assert_remote_timestamp('test', 5000)
        self.assert_remote_timestamp('hello', 6000)
        self.assert_remote_timestamp('baz', 8000)
        self.assert_contents('baz', b'just syncing some stuff')

        self.sync_clients()

    def test_three_way_sync(self):
        self.create_local_client()
        self.create_local_client()
        self.create_local_client()

        set_local_contents(self.folders[0], 'foo', timestamp=1000)
        set_local_contents(self.folders[1], 'bar', timestamp=2000, data='red')
        set_local_contents(self.folders[2], 'baz', timestamp=3000)

        self.sync_clients()

        self.assert_local_keys(['foo', 'bar', 'baz'])
        self.assert_contents('bar', b'red')
        self.assert_remote_timestamp('foo', 1000)
        self.assert_remote_timestamp('bar', 2000)
        self.assert_remote_timestamp('baz', 3000)

        set_local_contents(self.folders[1], 'bar', timestamp=8000, data='green')
        self.sync_clients()

        self.assert_local_keys(['foo', 'bar', 'baz'])
        self.assert_contents('bar', b'green')
        self.assert_remote_timestamp('foo', 1000)
        self.assert_remote_timestamp('bar', 8000)
        self.assert_remote_timestamp('baz', 3000)

        self.delete_local(self.folders[2], 'foo')
        self.sync_clients()

        self.assert_file_existence(['foo'], False)
        self.assert_local_keys(['bar', 'baz'])

        self.sync_clients()