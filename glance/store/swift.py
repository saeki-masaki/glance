# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010-2011 OpenStack, LLC
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Storage backend for SWIFT"""

from __future__ import absolute_import

import httplib
import logging
import urllib

from swift.common.client import Connection, ClientException

from glance.common import config
from glance.common import exception
import glance.store

DEFAULT_SWIFT_ACCOUNT = 'glance'
DEFAULT_SWIFT_CONTAINER = 'glance'

logger = logging.getLogger('glance.store.swift')


class SwiftBackend(glance.store.Backend):
    """
    An implementation of the swift backend adapter.
    """
    EXAMPLE_URL = "swift://<USER>:<KEY>@<AUTH_ADDRESS>/<CONTAINER>/<FILE>"

    CHUNKSIZE = 65536

    @classmethod
    def get(cls, parsed_uri, expected_size=None, options=None,
            conn_class=None):
        """
        Takes a parsed_uri in the format of:
        swift://user:password@auth_url/container/file.gz.0, connects to the
        swift instance at auth_url and downloads the file. Returns the
        generator resp_body provided by get_object.
        """
        (user, key, authurl, container, obj) = \
            cls._parse_swift_tokens(parsed_uri)

        # TODO(sirp): snet=False for now, however, if the instance of
        # swift we're talking to is within our same region, we should set
        # snet=True
        swift_conn = Connection(
            authurl=authurl, user=user, key=key, snet=False)

        try:
            (resp_headers, resp_body) = swift_conn.get_object(
                container=container, obj=obj, resp_chunk_size=cls.CHUNKSIZE)
        except ClientException, e:
            if e.http_status == httplib.NOT_FOUND:
                location = "swift://%s:%s@%s/%s/%s" % (user, key, authurl,
                                                    container, obj)
                raise exception.NotFound("Swift could not find image at "
                                         "location %(location)s" % locals())

        if expected_size:
            obj_size = int(resp_headers['content-length'])
            if  obj_size != expected_size:
                raise glance.store.BackendException(
                    "Expected %s byte file, Swift has %s bytes" %
                    (expected_size, obj_size))

        return resp_body

    @classmethod
    def add(cls, id, data, options, conn_class=None):
        """
        Stores image data to Swift and returns a location that the image was
        written to.

        Swift writes the image data using the scheme:
            ``swift://<USER>:<KEY>@<AUTH_ADDRESS>/<CONTAINER>/<ID>`
        where:
            <USER> = ``swift_store_user``
            <KEY> = ``swift_store_key``
            <AUTH_ADDRESS> = ``swift_store_auth_address``
            <CONTAINER> = ``swift_store_container``
            <ID> = The id of the image being added

        :param id: The opaque image identifier
        :param data: The image data to write, as a file-like object
        :param options: Conf mapping

        :retval Tuple with (location, size)
                The location that was written,
                and the size in bytes of the data written
        """
        account = options.get('swift_store_account',
                              DEFAULT_SWIFT_ACCOUNT)
        container = options.get('swift_store_container',
                                DEFAULT_SWIFT_CONTAINER)
        auth_address = options.get('swift_store_auth_address')
        user = options.get('swift_store_user')
        key = options.get('swift_store_key')

        # TODO(jaypipes): This needs to be checked every time
        # because of the decision to make glance.store.Backend's
        # interface all @classmethods. This is inefficient. Backend
        # should be a stateful object with options parsed once in
        # a constructor.
        if not auth_address:
            logger.error(msg)
            msg = ("Could not find swift_store_auth_address in configuration "
                   "options.")
            raise glance.store.BackendException(msg)
        else:
            full_auth_address = auth_address
            if not full_auth_address.startswith('http'):
                full_auth_address = 'https://' + full_auth_address

        if not user:
            logger.error(msg)
            msg = ("Could not find swift_store_user in configuration "
                   "options.")
            raise glance.store.BackendException(msg)

        if not key:
            logger.error(msg)
            msg = ("Could not find swift_store_key in configuration "
                   "options.")
            raise glance.store.BackendException(msg)

        swift_conn = Connection(authurl=full_auth_address, user=user,
                                      key=key, snet=False)

        logger.debug("Adding image object to Swift using "
                     "(auth_address=%(auth_address)s, user=%(user)s, "
                     "key=%(key)s)" % locals())

        create_container_if_missing(container, swift_conn, options)

        obj_name = str(id)
        try:
            obj_etag = swift_conn.put_object(container, obj_name, data)

            # NOTE: We return the user and key here! Have to because
            # location is used by the API server to return the actual
            # image data. We *really* should consider NOT returning
            # the location attribute from GET /images/<ID> and
            # GET /images/details
            location = "swift://%(user)s:%(key)s@%(auth_address)s/"\
                       "%(container)s/%(obj_name)s" % locals()

            # We do a HEAD on the newly-added image to determine the size
            # of the image. A bit slow, but better than taking the word
            # of the user adding the image with size attribute in the metadata
            resp_headers = swift_conn.head_object(container, obj_name)
            size = 0
            # header keys are lowercased by Swift
            if 'content-length' in resp_headers:
                size = int(resp_headers['content-length'])
            return (location, size)
        except ClientException, e:
            if e.http_status == httplib.CONFLICT:
                location = "swift://%s:%s@%s/%s/%s" % (user, key, auth_address,
                                                    container, id)
                raise exception.Duplicate("Swift already has an image at "
                                          "location %(location)s" % locals())
            msg = ("Failed to add object to Swift.\n"
                   "Got error from Swift: %(e)s" % locals())
            raise glance.store.BackendException(msg)

    @classmethod
    def delete(cls, parsed_uri, conn_class=None):
        """
        Deletes the swift object(s) at the parsed_uri location
        """
        (user, key, authurl, container, obj) = \
            cls._parse_swift_tokens(parsed_uri)

        # TODO(sirp): snet=False for now, however, if the instance of
        # swift we're talking to is within our same region, we should set
        # snet=True
        swift_conn = Connection(
            authurl=authurl, user=user, key=key, snet=False)

        try:
            swift_conn.delete_object(container, obj)
        except ClientException, e:
            if e.http_status == httplib.NOT_FOUND:
                location = "swift://%s:%s@%s/%s/%s" % (user, key, authurl,
                                                    container, obj)
                raise exception.NotFound("Swift could not find image at "
                                         "location %(location)s" % locals())

    @classmethod
    def _parse_swift_tokens(cls, parsed_uri):
        """
        Parsing the swift uri is three phases:
            1) urlparse to split the tokens
            2) use RE to split on @ and /
            3) reassemble authurl
        """
        path = parsed_uri.path.lstrip('//')
        netloc = parsed_uri.netloc

        try:
            try:
                creds, netloc = netloc.split('@')
                path = '/'.join([netloc, path])
            except ValueError:
                # Python 2.6.1 compat
                # see lp659445 and Python issue7904
                creds, path = path.split('@')

            user, key = creds.split(':')
            path_parts = path.split('/')
            obj = path_parts.pop()
            container = path_parts.pop()
        except (ValueError, IndexError):
            raise glance.store.BackendException(
                 "Expected four values to unpack in: swift:%s. "
                 "Should have received something like: %s."
                 % (parsed_uri.path, cls.EXAMPLE_URL))

        authurl = "https://%s" % '/'.join(path_parts)

        return user, key, authurl, container, obj


def create_container_if_missing(container, swift_conn, options):
    """
    Creates a missing container in Swift if the
    ``swift_store_create_container_on_put`` option is set.

    :param container: Name of container to create
    :param swift_conn: Connection to Swift
    :param options: Option mapping
    """
    try:
        swift_conn.head_container(container)
    except ClientException, e:
        if e.http_status == httplib.NOT_FOUND:
            add_container = config.get_option(options,
                                'swift_store_create_container_on_put',
                                type='bool', default=False)
            if add_container:
                try:
                    swift_conn.put_container(container)
                except ClientException, e:
                    msg = ("Failed to add container to Swift.\n"
                           "Got error from Swift: %(e)s" % locals())
                    raise glance.store.BackendException(msg)
            else:
                msg = ("The container %(container)s does not exist in "
                       "Swift. Please set the "
                       "swift_store_create_container_on_put option"
                       "to add container to Swift automatically."
                       % locals())
                raise glance.store.BackendException(msg)
