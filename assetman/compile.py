#!/bin/python

from __future__ import with_statement

import os
import sys
import re
import logging
import multiprocessing
import hashlib

from optparse import OptionParser

from assetman.manifest import Manifest 
from assetman.tools import iter_template_paths, get_static_pattern, make_static_path, get_parser
from assetman.compilers import DependencyError, ParseError, CompileError

from assetman.settings import Settings

parser = OptionParser(description='Compiles assets for AssetMan')

parser.add_option(
    '--template-dir', type="string", action='append', 
    help='Directory to crawl looking for static assets to compile.')

parser.add_option(
    '--template-ext', type="string", default="html",
    help='File extension of compilable templates.')

parser.add_option(
    '--static-dir', type="string", default='static',
    help='Directory where static assets are located in this project.')

parser.add_option(
    '--output-dir', type="string", default='assets',
    help='Directory where compiled assets are to be placed.')

parser.add_option(
    '--static-url-path', type="string", default="/",
    help="Static asset base url path")

parser.add_option(
    '--compiled-manifest-path', type="string", default="/",
    help="Location to read/write the compiled asset manifest")

parser.add_option(
    '-t', '--test-needs-compile', action="store_true",
    help='Check whether a compile is needed. Exits 1 if so.')

parser.add_option(
    '-f', '--force', action="store_true",
    help='Force a recompile of everything.')

parser.add_option(
    '-u', '--skip-upload', action="store_true",
    help='Do not upload anything to S3.')

parser.add_option(
    '-i', '--skip-inline-images', action="store_true",
    help='Do not sub data URIs for small images in CSS.')


# Static calls are like {{ assetman.static_url('path.jpg') }} or include
# an extra arg: {{ assteman.static_url('path.jpg', local=True) }}
static_url_call_finder = re.compile(r'assetman\.static_url\((.*?)(,.*?)?\)').finditer

##############################################################################
# Multiprocessing workers
##############################################################################
class ParserWorker(object):
    def __init__(self, settings):
        self.settings = settings

    def __call__(self, template_path):
        """Takes a template path and returns a list of AssetCompiler instances
        extracted from that template. Helper function to be called by each process
        in the process pool created by find_assetman_compilers, above.
        """
        template = get_parser(template_path, self.settings)
        return list(template.get_compilers())

class CompileWorker(object):
    """Takes an AssetCompiler and, based on the manifest, compiles the assets,
    writing the results to disk. Used as a helper function when compiling
    assets in parallel.
    """
    def __init__(self, skip_inline_images, manifest):
        self.manifest = manifest
        self.skip_inline_images = skip_inline_images

    def __call__(self, compiler):
        with open(compiler.get_compiled_path(), 'w') as outfile:
            outfile.write(compiler.compile(self.manifest, self.skip_inline_images))

##############################################################################
# Compiler support functions
##############################################################################
def get_file_hash(path, block_size=8192):
    """Calculates the content hash for the file at the given path."""
    md5 = hashlib.md5()
    with open(path, 'rb') as f:
        while True:
            block = f.read(block_size)
            if not block:
                break
            md5.update(block)
    return md5.hexdigest()

##############################################################################
# Build and compare dependency manifests
##############################################################################
def static_finder(s, static_url_prefix):
    pattern = get_static_pattern(static_url_prefix)
    return re.compile(pattern).finditer(s)

def import_finder(s):
    return re.compile(r"""@import (url\()?(["'])(.*?)(\2)""").finditer(s)

def empty_asset_entry():
    """This is the form of each 'assets' entry in the manifest as we're
    building it.
    """
    return {
        'version': None,
        'versioned_path': None,
        'deps': set()
    }

def build_compilers(paths, settings):
    """Parse each template and return a list of AssetCompiler instances for
    any assetman.include_* blocks in each template.
    """
    # pool.map will not ordinarily handle KeyboardInterrupts cleanly,
    # but if you give them a timeout they will. More info:
    # http://bugs.python.org/issue8296
    # http://stackoverflow.com/a/1408476/151221
    pool = multiprocessing.Pool()

    parser_worker = ParserWorker(settings)
    return [x for xs in pool.map_async(parser_worker, paths).get(1e100) for x in xs]

def iter_template_deps(static_dir, src_path):
    """Yields static resources included as {{assetman.static_url()}} calls
    in the given source path, which should be a Tornado template.
    """
    src = open(src_path).read()
    for match in static_url_call_finder(src):
        arg = match.group(1)
        quotes = '\'"'
        if arg[0] not in quotes or arg[-1] not in quotes:
            msg = 'Vars not allowed in static_url calls: %s' % match.group(0)
            raise ParseError(src_path, msg)
        else:
            dep_path = make_static_path(static_dir, arg.strip(quotes))
            if os.path.isfile(dep_path):
                yield dep_path
            else:
                logging.warn('Missing dep %s (src: %s)', dep_path, src_path)

###############################################################################

def version_dependency(path, manifest):
    """A dependency's version is calculated using this recursive formula:

        version = md5(md5(path_contents) + version(deps))

    So, the version of a path is based on the hash of its own file contents as
    well as those of each of its dependencies.
    """
    assert path in manifest['assets'], path
    assert os.path.isfile(path), path
    if manifest['assets'][path]['version']:
        return manifest['assets'][path]['version']
    h = hashlib.md5()
    h.update(get_file_hash(path))
    for dep_path in manifest['assets'][path]['deps']:
        h.update(version_dependency(dep_path, manifest))
    version = h.hexdigest()
    _, ext = os.path.splitext(path)
    manifest['assets'][path]['version'] = version
    manifest['assets'][path]['versioned_path'] = version + ext
    return manifest['assets'][path]['version']

def normalize_manifest(manifest):
    """Normalizes and sanity-checks the given dependency manifest by first
    ensuring that all deps are expressed as lists instead of sets (as they are
    when the manifest is built) and then by ensuring that every dependency has
    its own entry and version in the top level of the manifest.
    """
    for parent, depspec in manifest['assets'].iteritems():
        depspec['deps'] = list(depspec['deps'])
        for dep in depspec['deps']:
            assert dep in manifest['assets'], (parent, dep)
            assert depspec['version'], (parent, dep)
    for name_hash, depspec in manifest['blocks'].iteritems():
        assert depspec['version'], name_hash
    return manifest

def iter_deps(static_dir, src_path, static_url_prefix):
    """Yields first-level dependencies from the given source path."""
    assert os.path.isfile(src_path), src_path
    dep_iter = {
        '.js': iter_js_deps,
        '.css': iter_css_deps,
        '.less': iter_css_deps,
        '.scss': iter_css_deps,
        '.html': iter_template_deps,
        }.get(os.path.splitext(src_path)[1])
    if dep_iter:
        for path in dep_iter(static_dir, src_path, static_url_prefix):
            yield path

def iter_css_deps(static_dir, src_path, static_url_prefix):
    """Yields first-level dependencies from the given source path, which
    should be a CSS, Less, or Sass file. Dependencies will either be more
    CSS/Less/Sass files or static image resources.
    """
    assert os.path.isfile(src_path), src_path
    root = os.path.dirname(src_path)
    src = open(src_path).read()

    # First look for CSS/Less imports and recursively descend into them
    for match in import_finder(src):
        path = match.group(3)
        if src_path.endswith('.less') and os.path.splitext(path)[1] == '':
            path = path + '.less'
        # normpath will take care of '../' path components
        new_root = os.path.normpath(os.path.join(root, os.path.dirname(path)))
        full_path = os.path.join(new_root, os.path.basename(path))
        assert os.path.isdir(new_root), new_root
        assert os.path.isfile(full_path), full_path
        yield full_path

    # Then look for static assets (images, basically)
    for dep in iter_static_deps(static_dir, src_path, static_url_prefix):
        yield dep

def iter_js_deps(static_dir, src_path, static_url_prefix):
    """Yields first-level dependencies from the given source path, which
    should be a JavaScript file. Dependencies will be static image resources.
    """
    return iter_static_deps(static_dir, src_path, static_url_prefix)

def iter_static_deps(static_dir, src_path, static_url_prefix):
    """Yields static resources (ie, image files) from the given source path.
    """
    assert os.path.isfile(src_path), src_path
    for match in static_finder(open(src_path).read(), static_url_prefix):
        dep_path = make_static_path(static_dir, match.group(2))
        if os.path.isfile(dep_path):
            yield dep_path
        else:
            logging.warn('Missing dep %s (src: %s)', dep_path, src_path)


def _build_manifest_helper(static_dir, src_paths, static_url_prefix, manifest):
    assert isinstance(src_paths, (list, tuple))
    assert isinstance(manifest, dict)
    assert 'assets' in manifest and isinstance(manifest['assets'], dict)
    for src_path in src_paths:
        # Make sure every source path at least has the skeleton entry
        manifest['assets'].setdefault(src_path, empty_asset_entry())
        for dep_path in iter_deps(static_dir, src_path, static_url_prefix):
            manifest['assets'][src_path]['deps'].add(dep_path)
            _build_manifest_helper(static_dir, [dep_path], static_url_prefix, manifest)

def build_manifest(paths, settings):
    """Recursively builds the dependency manifest for the given list of source
    paths.
    """
    assert isinstance(paths, (list, tuple))

    # First, parse each template to build a list of AssetCompiler instances
    compilers = build_compilers(paths, settings)

    # Add each AssetCompiler's paths to our set of paths to search for deps
    paths = set(paths)
    for compiler in compilers:
        paths.update(compiler.get_paths())
    paths = list(paths)

    # Start building the new manifest
    manifest = Manifest(settings).make_empty_manifest()
    _build_manifest_helper(settings['static_dir'], paths, settings['static_url_prefix'], manifest)
    assert all(path in manifest['assets'] for path in paths)

    # Next, calculate the version hash for each entry in the manifest
    for src_path in manifest['assets']:
        version_dependency(src_path, manifest)

    # Normalize and validate the manifest
    normalize_manifest(manifest)

    # Update the 'blocks' section of the manifest for each asset block
    for compiler in compilers:
        name_hash = compiler.get_hash()
        content_hash = compiler.get_current_content_hash(manifest)
        manifest['blocks'][name_hash] = {
            'version': content_hash,
            'versioned_path': content_hash + '.' + compiler.get_ext(),
        }

    return manifest, compilers

def _create_settings(options):
    return Settings(compiled_asset_root=options.output_dir,
                    static_dir=options.static_dir,
                    static_url_prefix=options.static_url_path,
                    compiled_manifest_path=options.compiled_manifest_path,
                    template_dirs=options.template_dir,
                    template_extension=options.template_ext)

def main(options):
    settings = _create_settings(options) 

    if not re.match(r'^/.*?/$', settings.get('static_url_prefix')):
        logging.error('static_url_prefix setting must begin and end with a slash')
        sys.exit(1)

    if not os.path.isdir(settings['compiled_asset_root']) and not options.test_needs_compile:
        logging.info('Creating output directory: %s', settings['compiled_asset_root'])
        os.makedirs(settings['compiled_asset_root'])

    for d in settings['template_dirs']:
        if not os.path.isdir(d):
            logging.error('Template directory not found: %r', d)
            return 1

    if not os.path.isdir(settings['static_dir']):
        logging.error('Static directory not found: %r', settings['static_dir'])
        return 1

    # Find all the templates we need to parse
    paths = list(iter_template_paths(settings['template_dirs'], settings['template_extension']))

    # Load the current manifest and generate a new one
    cached_manifest = Manifest(settings).load_manifest()
    try:
        current_manifest, compilers = build_manifest(paths, settings)
    except ParseError, e:
        src_path, msg = e.args
        logging.error('Error parsing template %s', src_path)
        logging.error(msg)
        return 1
    except DependencyError, e:
        src_path, missing_deps = e.args
        logging.error('Dependency error in %s!', src_path)
        logging.error('Missing paths: %s', missing_deps)
        return 1

    # Remove duplicates from our list of compilers. This de-duplication must
    # happen after the current manifest is built, because each non-unique
    # compiler's source path figures into the dependency tracking. But we only
    # need to actually compile each block once.
    logging.debug('Found %d assetman block compilers', len(compilers))
    compilers = dict((c.get_hash(), c) for c in compilers).values()
    logging.debug('%d unique assetman block compilers', len(compilers))

    # update the manifest on each our compilers to reflect the new manifest,
    # which is needed to know the output path for each compiler.
    for compiler in compilers:
        compiler.manifest = current_manifest

    # Figure out which asset blocks need to be (re)compiled, if any.
    def needs_compile(compiler):
        return compiler.needs_compile(cached_manifest, current_manifest)

    if options.force:
        to_compile = compilers
    else:
        to_compile = filter(needs_compile, compilers)

    # Figure out if any static assets referenced in the new manifest are
    # missing from the cached manifest.
    def assets_in_sync(asset):
        if asset not in cached_manifest['assets']:
            logging.warn('Static asset %s not in cached manifest', asset)
            return False
        if cached_manifest['assets'][asset]['version'] != current_manifest['assets'][asset]['version']:
            logging.warn('Static asset %s version mismatch', asset)
            return False
        return True

    assets_out_of_sync = not all(map(assets_in_sync, current_manifest['assets']))
    if assets_out_of_sync:
        logging.warn('Static assets out of sync')

    if to_compile or assets_out_of_sync:
        # If we're only testing whether a compile is needed, we're done
        if options.test_needs_compile:
            return 1

        pool = multiprocessing.Pool()
        try:
            # See note above about bug in pool.map w/r/t KeyboardInterrupt.
            _compile_worker = CompileWorker(options.skip_inline_images, current_manifest)
            pool.map_async(_compile_worker, to_compile).get(1e100)
        except CompileError, e:
            cmd, msg = e.args
            logging.error('Compile error!')
            logging.error('Command: %s', ' '.join(cmd))
            logging.error('Error:   %s', msg)
            return 1
        except KeyboardInterrupt:
            logging.error('Interrupted by user, exiting...')
            return 1

        current_manifest.write()

    return 0

if __name__ == '__main__':
    options, args = parser.parse_args()
    sys.exit(main(options))