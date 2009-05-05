# -*- Mode: Python; test-case-name: morituri.test.test_header -*-
# vi:si:et:sw=4:sts=4:ts=4

# Morituri - for those about to RIP

# Copyright (C) 2009 Thomas Vander Stichele

# This file is part of morituri.
# 
# morituri is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# morituri is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with morituri.  If not, see <http://www.gnu.org/licenses/>.

import os
import sys
import tempfile
import optparse
import pickle
import shutil

import gobject
gobject.threads_init()

from morituri.common import common, task, checksum
from morituri.image import image, cue, table
from morituri.program import cdrdao, cdparanoia

"""
Rip a disc.
"""

from morituri.common import log
log.init()

def gtkmain(runner, taskk):
    import gtk
    runner.connect('stop', lambda _: gtk.main_quit())

    window = gtk.Window()
    window.add(runner)
    window.show_all()

    runner.run(taskk)

    gtk.main()

def climain(runner, taskk):
    runner.run(taskk)


def arcs(runner, function, table, track, offset):
    # rips the track with the given offset, return the arcs checksum
    print 'ripping track %r with offset %d' % (track, offset)

    fd, path = tempfile.mkstemp(suffix='.track%02d.offset%d.morituri.wav' % (
        track, offset))
    os.close(fd)

    track = table.tracks[track - 1]
    t = cdparanoia.ReadTrackTask(path, table, track.start, track.end, offset)
    t.description = 'Ripping with offset %d' % offset
    function(runner, t)

    t = checksum.AccurateRipChecksumTask(path, trackNumber=track,
        trackCount=len(table.tracks))
    function(runner, t)
    
    # os.unlink(path)
    return "%08x" % t.checksum
 
def main(argv):
    parser = optparse.OptionParser()

    default = 'cli'
    parser.add_option('-r', '--runner',
        action="store", dest="runner",
        help="runner ('cli' or 'gtk', defaults to %s)" % default,
        default=default)
    default = 0
    parser.add_option('-o', '--offset',
        action="store", dest="offset",
        help="sample offset (defaults to %d)" % default,
        default=default)
    parser.add_option('-t', '--table-pickle',
        action="store", dest="table_pickle",
        help="pickle to use for reading and writing the table",
        default=default)
    parser.add_option('-T', '--toc-pickle',
        action="store", dest="toc_pickle",
        help="pickle to use for reading and writing the TOC",
        default=default)

    options, args = parser.parse_args(argv[1:])

    if options.runner == 'cli':
        runner = task.SyncRunner()
        function = climain
    elif options.runner == 'gtk':
        from morituri.common import taskgtk
        runner = taskgtk.GtkProgressRunner()
        function = gtkmain

    # first, read the normal TOC and full index table
    ptoc = common.Persister(options.toc_pickle or None)
    if not ptoc.object:
        t = cdrdao.ReadTOCTask()
        function(runner, t)
        ptoc.persist(t.table)
    ittoc = ptoc.object

    ptable = common.Persister(options.table_pickle or None)
    if not ptable.object:
        t = cdrdao.ReadIndexTableTask()
        function(runner, t)
        ptable.persist(t.table)
    itable = ptable.object

    assert itable.hasTOC()

    lastTrackStart = 0

    for i, track in enumerate(itable.tracks):
        path = 'track%02d.wav' % (i + 1)
        # FIXME: optionally allow overriding reripping
        if not os.path.exists(path):
            print 'Ripping track %d' % (i + 1)
            t = cdparanoia.ReadVerifyTrackTask(path, ittoc, ittoc.getTrackStart(i + 1),
                ittoc.getTrackEnd(i + 1),
                offset=int(options.offset))
            t.description = 'Reading Track %d' % (i + 1)
            function(runner, t)
            if t.checksum:
                print 'Checksums match for track %d' % (i + 1)

        # overlay this rip onto the IndexTable
        itable.setFile(i + 1, 1, path, ittoc.getTrackLength(i + 1))

    print itable.tracks
    for t in itable.tracks:
        print t, t.indexes.values()

    handle = open('morituri.cue', 'w')
    handle.write(itable.cue())
    handle.close()

    # verify using accuraterip
    print "CDDB disc id", itable.getCDDBDiscId()
    url = itable.getAccurateRipURL()
    print "AccurateRip URL", url

    # FIXME: download url as a task too
    responses = []
    import urllib2
    try:
        handle = urllib2.urlopen(url)
        data = handle.read()
        responses = image.getAccurateRipResponses(data)
    except urllib2.HTTPError, e:
        if e.code == 404:
            print 'Album not found in AccurateRip database'
        else:
            raise

    if responses:
        print '%d AccurateRip reponses found' % len(responses)

        if responses[0].cddbDiscId != itable.getCDDBDiscId():
            print "AccurateRip response discid different: %s" % \
                responses[0].cddbDiscId

       
    # FIXME: put accuraterip verification into a separate task/function
    # and apply here
    cueImage = image.Image('morituri.cue')
    verifytask = image.ImageVerifyTask(cueImage)
    cuetask = image.AccurateRipChecksumTask(cueImage)
    function(runner, verifytask)
    function(runner, cuetask)

    response = None # track which response matches, for all tracks

    # loop over tracks
    for i, checksum in enumerate(cuetask.checksums):
        status = 'rip NOT accurate'

        confidence = None
        archecksum = None

        # match against each response's checksum
        for j, r in enumerate(responses):
            if "%08x" % checksum == r.checksums[i]:
                if not response:
                    response = r
                else:
                    assert r == response, \
                        "checksum %s for %d matches wrong response %d, "\
                        "checksum %s" % (
                            checksum, i + 1, j + 1, response.checksums[i])
                status = 'rip accurate    '
                archecksum = checksum
                confidence = response.confidences[i]

        c = "(not found)"
        ar = "(not in database)"
        if responses:
            if not response:
                print 'ERROR: none of the responses matched.'
            else:
                maxConfidence = max(r.confidences[i] for r in responses)
                     
                c = "(confidence %3d)" % maxConfidence
                if confidence is not None:
                    if confidence < maxConfidence:
                        c = "(confidence %3d of %3d)" % (confidence, maxConfidence)

                ar = ", AR [%s]" % response.checksums[i]
        print "Track %2d: %s %s [%08x]%s" % (
            i + 1, status, c, checksum, ar)




main(sys.argv)
