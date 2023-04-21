# Copyright 2012-2013 Amazon.com, Inc. or its affiliates. All Rights Reserved.

# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at

#     http://aws.amazon.com/apache2.0/

# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
import logging
from datetime import datetime

from botocore.compat import json
from botocore.utils import set_value_from_jmespath
from botocore.paginate import PageIterator
from ruamel.yaml import YAML

from awscli.table import MultiTable, Styler, ColorizedStyler
from awscli import text
from awscli import compat
from awscli.utils import json_encoder


LOG = logging.getLogger(__name__)


def is_response_paginated(response):
    return isinstance(response, PageIterator)


class Formatter(object):
    def __init__(self, args):
        self._args = args

    def _get_transformed_response_for_output(self, response):
        # Performs various transformations to the API response ready to
        # be outputted such as removing response id's and performing the
        # query
        self._remove_request_id(response)
        return self._apply_query_if_needed(response)

    def _remove_request_id(self, response_data):
        if 'ResponseMetadata' in response_data:
            if 'RequestId' in response_data['ResponseMetadata']:
                request_id = response_data['ResponseMetadata']['RequestId']
                LOG.debug('RequestId: %s', request_id)
            del response_data['ResponseMetadata']

    def _apply_query_if_needed(self, response_data):
        if self._args.query is not None:
            response_data = self._args.query.search(response_data)
        return response_data

    def _get_default_stream(self):
        return compat.get_stdout_text_writer()

    def _flush_stream(self, stream):
        try:
            stream.flush()
        except IOError:
            pass


class FullyBufferedFormatter(Formatter):
    def __call__(self, command_name, response, stream=None):
        if stream is None:
            # Retrieve stdout on invocation instead of at import time
            # so that if anything wraps stdout we'll pick up those changes
            # (specifically colorama on windows wraps stdout).
            stream = self._get_default_stream()
        # I think the interfaces between non-paginated
        # and paginated responses can still be cleaned up.
        if is_response_paginated(response):
            response_data = response.build_full_result()
        else:
            response_data = response
        response_data = self._get_transformed_response_for_output(
            response_data)
        try:
            self._format_response(command_name, response_data, stream)
        except IOError as e:
            # If the reading end of our stdout stream has closed the file
            # we can just exit.
            pass
        finally:
            # flush is needed to avoid the "close failed in file object
            # destructor" in python2.x (see http://bugs.python.org/issue11380).
            self._flush_stream(stream)


class JSONFormatter(FullyBufferedFormatter):

    def _format_response(self, command_name, response, stream):
        # For operations that have no response body (e.g. s3 put-object)
        # the response will be an empty string.  We don't want to print
        # that out to the user but other "falsey" values like an empty
        # dictionary should be printed.
        if response != {}:
            json.dump(response, stream, indent=4, default=json_encoder,
                    ensure_ascii=False)
            stream.write('\n')


class YAMLDumper(object):
    def __init__(self):
        self._yaml = YAML(typ='safe')
        # Encoding is set to None because we handle the encoding by
        # wrapping the stream, so there's no need for the yaml library
        # to do it.
        self._yaml.encoding = None
        self._yaml.representer.default_flow_style = False

    def dump(self, value, stream):
        if self._is_json_scalar(value) or isinstance(value, datetime):
            # YAML will attempt to disambiguate scalars by ending the stream
            # with an elipsis. While this is technically valid YAML,
            # it's not particularly useful. Unfortunately there's no
            # universal way around this, so instead we just json dump the
            # values. Also note that datetimes are explicitly not supported
            # - the json dumper will complain if you pass them in. datetime
            # values should respect the cli timestamp format, which is
            # impossible to do from the Formatter.
            json.dump(value, stream, ensure_ascii=False, default=json_encoder)
            stream.write('\n')
        else:
            self._yaml.dump(value, stream)

    def _is_json_scalar(self, value):
        if value is None:
            return True
        return isinstance(value, (int, float, bool, compat.six.string_types))


class YAMLFormatter(FullyBufferedFormatter):
    def __init__(self, args, yaml_dumper=None):
        super(YAMLFormatter, self).__init__(args)
        self._yaml_dumper = yaml_dumper
        if yaml_dumper is None:
            self._yaml_dumper = YAMLDumper()

    def _format_response(self, command_name, response, stream):
        if response == {}:
            return None
        self._yaml_dumper.dump(response, stream)


class StreamedYAMLFormatter(Formatter):
    def __init__(self, args, yaml_dumper=None):
        super(StreamedYAMLFormatter, self).__init__(args)
        self._yaml_dumper = yaml_dumper
        if yaml_dumper is None:
            self._yaml_dumper = YAMLDumper()

    def __call__(self, command_name, response, stream=None):
        if stream is None:
            stream = self._get_default_stream()
        response_stream = self._get_response_stream(response)
        for response in response_stream:
            try:
                # For YAML it is ambiguous as to whether the output from the
                # stream is N responses in 1 list or N lists each with 1
                # response. We go with the latter so we can reuse our YAML
                # dumper
                self._yaml_dumper.dump([response], stream)
            except IOError:
                # If the reading end of our stdout stream has closed the file
                # we can just exit.
                return
            finally:
                # flush is needed to avoid the "close failed in file object
                # destructor" in python2.x. See:
                # http://bugs.python.org/issue11380).
                self._flush_stream(stream)

    def _get_response_stream(self, response):
        if is_response_paginated(response):
            return compat.imap(
                self._get_transformed_response_for_output, response)
        else:
            output = self._get_transformed_response_for_output(response)
            if output == {}:
                # The operation did not have an output so return an empty list
                # as the stream so nothing gets printed out.
                return []
            return [output]


class TableFormatter(FullyBufferedFormatter):
    """Pretty print a table from a given response.

    The table formatter is able to take any generic response
    and generate a pretty printed table.  It does this without
    using the output definition from the model.

    """
    def __init__(self, args, table=None):
        super(TableFormatter, self).__init__(args)
        if args.color == 'auto':
            self.table = MultiTable(initial_section=False,
                                    column_separator='|')
        elif args.color == 'off':
            styler = Styler()
            self.table = MultiTable(initial_section=False,
                                    column_separator='|', styler=styler)
        elif args.color == 'on':
            styler = ColorizedStyler()
            self.table = MultiTable(initial_section=False,
                                    column_separator='|', styler=styler)
        else:
            raise ValueError("Unknown color option: %s" % args.color)

    def _format_response(self, command_name, response, stream):
        if self._build_table(command_name, response):
            try:
                self.table.render(stream)
            except IOError:
                # If they're piping stdout to another process which exits before
                # we're done writing all of our output, we'll get an error about a
                # closed pipe which we can safely ignore.
                pass

    def _build_table(self, title, current, indent_level=0):
        if not current:
            return False
        if title is not None:
            self.table.new_section(title, indent_level=indent_level)
        if isinstance(current, list):
            if isinstance(current[0], dict):
                self._build_sub_table_from_list(current, indent_level, title)
            else:
                for item in current:
                    if self._scalar_type(item):
                        self.table.add_row([item])
                    elif all(self._scalar_type(el) for el in item):
                        self.table.add_row(item)
                    else:
                        self._build_table(title=None, current=item)
        if isinstance(current, dict):
            # Render a single row section with keys as header
            # and the row as the values, unless the value
            # is a list.
            self._build_sub_table_from_dict(current, indent_level)
        return True

    def _build_sub_table_from_dict(self, current, indent_level):
        # Render a single row section with keys as header
        # and the row as the values, unless the value
        # is a list.
        headers, more = self._group_scalar_keys(current)
        if len(headers) == 1:
            # Special casing if a dict has a single scalar key/value pair.
            self.table.add_row([headers[0], current[headers[0]]])
        elif headers:
            self.table.add_row_header(headers)
            self.table.add_row([current[k] for k in headers])
        for remaining in more:
            self._build_table(remaining, current[remaining],
                              indent_level=indent_level + 1)

    def _build_sub_table_from_list(self, current, indent_level, title):
        headers, more = self._group_scalar_keys_from_list(current)
        self.table.add_row_header(headers)
        first = True
        for element in current:
            if not first and more:
                self.table.new_section(title,
                                       indent_level=indent_level)
                self.table.add_row_header(headers)
            first = False
            # Use .get() to account for the fact that sometimes an element
            # may not have all the keys from the header.
            self.table.add_row([element.get(header, '') for header in headers])
            for remaining in more:
                # Some of the non scalar attributes may not necessarily
                # be in every single element of the list, so we need to
                # check this condition before recursing.
                if remaining in element:
                    self._build_table(remaining, element[remaining],
                                    indent_level=indent_level + 1)

    def _scalar_type(self, element):
        return not isinstance(element, (list, dict))

    def _group_scalar_keys_from_list(self, list_of_dicts):
        # We want to make sure we catch all the keys in the list of dicts.
        # Most of the time each list element has the same keys, but sometimes
        # a list element will have keys not defined in other elements.
        headers = set()
        more = set()
        for item in list_of_dicts:
            current_headers, current_more = self._group_scalar_keys(item)
            headers.update(current_headers)
            more.update(current_more)
        headers = list(sorted(headers))
        more = list(sorted(more))
        return headers, more

    def _group_scalar_keys(self, current):
        # Given a dict, separate the keys into those whose values are
        # scalar, and those whose values aren't.  Return two lists,
        # one is the scalar value keys, the second is the remaining keys.
        more = []
        headers = []
        for element in current:
            if self._scalar_type(current[element]):
                headers.append(element)
            else:
                more.append(element)
        headers.sort()
        more.sort()
        return headers, more


class TextFormatter(Formatter):

    def __call__(self, command_name, response, stream=None):
        if stream is None:
            stream = self._get_default_stream()
        try:
            if is_response_paginated(response):
                result_keys = response.result_keys
                for i, page in enumerate(response):
                    if i > 0:
                        current = {}
                    else:
                        current = response.non_aggregate_part

                    for result_key in result_keys:
                        data = result_key.search(page)
                        set_value_from_jmespath(
                            current,
                            result_key.expression,
                            data
                        )
                    self._format_response(current, stream)
                if response.resume_token:
                    # Tell the user about the next token so they can continue
                    # if they want.
                    self._format_response(
                        {'NextToken': {'NextToken': response.resume_token}},
                        stream)
            else:
                self._remove_request_id(response)
                self._format_response(response, stream)
        finally:
            # flush is needed to avoid the "close failed in file object
            # destructor" in python2.x (see http://bugs.python.org/issue11380).
            self._flush_stream(stream)

    def _format_response(self, response, stream):
        if self._args.query is not None:
            expression = self._args.query
            response = expression.search(response)
        text.format_text(response, stream)


CLI_OUTPUT_FORMATS = {
    'json': JSONFormatter,
    'text': TextFormatter,
    'table': TableFormatter,
    'yaml': YAMLFormatter,
    'yaml-stream': StreamedYAMLFormatter,
}


def get_formatter(format_type, args):
    if format_type not in CLI_OUTPUT_FORMATS:
        raise ValueError("Unknown output type: %s" % format_type)
    format_type_cls = CLI_OUTPUT_FORMATS[format_type]
    return format_type_cls(args)
