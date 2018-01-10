#!/usr/bin/env python

__author__    = 'Gilles Boccon-Gibod (bok@bok.net)'
__copyright__ = 'Copyright 2011-2012 Axiomatic Systems, LLC.'

###
# NOTE: this script needs Bento4 command line binaries to run
# You must place the 'mp4info' and 'mp4encrypt' binaries
# in a directory named 'bin/<platform>' at the same level as where
# this script is.
# <platform> depends on the platform you're running on:
# Mac OSX   --> platform = macosx
# Linux x86 --> platform = linux-x86
# Windows   --> platform = win32

### Imports

from optparse import OptionParser, make_option, OptionError
from mp4utils import *
import urllib2
import urlparse
import shutil
import itertools
import json
import sys
import mp4tohls
import platform
import math
from xml.etree import ElementTree
from subprocess import check_output, CalledProcessError

# setup main options
SCRIPT_PATH = path.abspath(path.dirname(__file__))
sys.path += [SCRIPT_PATH]

# constants
DASH_NS_URN_COMPAT = 'urn:mpeg:DASH:schema:MPD:2011'
DASH_NS_URN        = 'urn:mpeg:dash:schema:mpd:2011'
DASH_NS_COMPAT     = '{'+DASH_NS_URN_COMPAT+'}'
DASH_NS            = '{'+DASH_NS_URN+'}'
MARLIN_MAS_NS_URN  = 'urn:marlin:mas:1-0:services:schemas:mpd'
MARLIN_MAS_NS      = '{'+MARLIN_MAS_NS_URN+'}'

def Bento4Command(name, *args, **kwargs):
    cmd = [os.path.join(Options.exec_dir, name)]
    for kwarg in kwargs:
        arg = kwarg.replace('_', '-')
        cmd.append('--'+arg)
        if not isinstance(kwargs[kwarg], bool):
            cmd.append(kwargs[kwarg])
    cmd += args
    #print cmd
    try:
        return check_output(cmd) 
    except CalledProcessError, e:
        #print e
        raise Exception("binary tool failed with error %d" % e.returncode)
    
def Mp4Info(filename, **args):
    return Bento4Command('mp4info', filename, **args)

def GetTrackIds(mp4):
    track_ids = []
    json_info = Mp4Info(mp4, format='json')
    info = json.loads(json_info, strict=False)

    for track in info['tracks']:
        track_ids.append(track['id'])
        
    return track_ids

def ProcessUrlTemplate(template, representation_id, bandwidth, time, number):
    if representation_id is not None: result = template.replace('$RepresentationID$', representation_id)
    if number is not None:
        nstart = result.find('$Number')
        if nstart >= 0:
            nend = result.find('$', nstart+1)
            if nend >= 0:
                var = result[nstart+1 : nend]
                if 'Number%' in var:
                    value = var[6:] % (int(number))
                else:
                    value = number
                result = result.replace('$'+var+'$', value)
    if bandwidth is not None:         result = result.replace('$Bandwidth$', bandwidth)
    if time is not None:              result = result.replace('$Time$', time)
    result = result.replace('$$', '$')
    return result

def GetMpdSources(xml):
    mpd_tree = ElementTree.fromstring(xml)
    return GetMpdSourceFromChild(mpd_tree)

def GetMpdSourceFromChild(child):
    mpd_sources = []
    
    for c in child:
        if 'BaseURL' in c.tag:
            mpd_sources.append(c.text)
        else:
            mpd_sources += GetMpdSourceFromChild(c)

    return mpd_sources
    
class DashSegmentBaseInfo:
    def __init__(self, xml):
        self.initialization = None
        self.type = None
        for type in ['SegmentBase', 'SegmentTemplate', 'SegmentList']:
            e = xml.find(DASH_NS+type)
            if e is not None:
                self.type = type
                
                # parse common elements
                
                # type specifics
                if type == 'SegmentBase' or type == 'SegmentList':
                    init = e.find(DASH_NS+'Initialization')
                    if init is not None:
                        self.initialization = init.get('sourceURL')
                    
                if type == 'SegmentTemplate':
                    self.initialization = e.get('initialization')
                    self.media          = e.get('media')
                    self.timescale      = e.get('timescale')
                    self.startNumber    = e.get('startNumber')
                    
                # segment timeline
                st = e.find(DASH_NS+'SegmentTimeline')
                if st is not None:
                    self.segment_timeline = []
                    entries = st.findall(DASH_NS+'S')
                    for entry in entries:
                        item = {}
                        s_t = entry.get('t')
                        if s_t is not None:
                            item['t'] = int(s_t)
                        s_d = entry.get('d')
                        if s_d is not None:
                            item['d'] = int(s_d)
                        s_r = entry.get('r')
                        if s_r is not None:
                            item['r'] = int(s_r)
                        else:
                            item['r'] = 0
                        self.segment_timeline.append(item)
    
                break
                   
class DashRepresentation:
    def __init__(self, xml, parent):
        self.xml = xml
        self.parent = parent
        self.init_segment_url = None
        self.segment_urls = []
        self.segment_base = DashSegmentBaseInfo(xml)
        self.duration = 0
        
        # parse standard attributes
        self.bandwidth = xml.get('bandwidth')
        self.id        = xml.get('id')
                    
        # compute the segment base type
        node = self
        self.segment_base_type = None
        while node is not None:
            if node.segment_base.type in ['SegmentTemplate', 'SegmentList']:
                self.segment_base_type = node.segment_base.type
                break                
            node = node.parent            
        
        # compute the init segment URL
        self.ComputeInitSegmentUrl()
        
    def SegmentBaseLookup(self, field):
        node = self
        while node is not None:
            if field in node.segment_base.__dict__:
                return node.segment_base.__dict__[field]
            node = node.parent
        return None

    def AttributeLookup(self, field):
        node = self
        while node is not None:
            if field in node.__dict__:
                return node.__dict__[field]
            node = node.parent
        return None
        
    def ComputeInitSegmentUrl(self):
        node = self
        while node is not None:
            if node.segment_base.initialization is not None:
                self.initialization = node.segment_base.initialization
                break                
            node = node.parent
            
        self.init_segment_url = ProcessUrlTemplate(self.initialization, representation_id=self.id, bandwidth=self.bandwidth, time=None, number=None)
                    
    def GenerateSegmentUrls(self):
        if self.segment_base_type == 'SegmentTemplate':
            return self.GenerateSegmentUrlsFromTemplate()
        else:
            return self.GenerateSegmentUrlsFromList()
            
    def GenerateSegmentUrlsFromTemplate(self):
        media = self.SegmentBaseLookup('media')
        if media is None:
            print 'WARNING: no media attribute found for representation'
            return
        
        timeline = self.SegmentBaseLookup('segment_timeline')
        if timeline is None:
            start = self.SegmentBaseLookup('startNumber')
            if start is None:
                current_number = 1
            else:
                current_number = int(start)
            while True:
                url = ProcessUrlTemplate(media, representation_id=self.id, bandwidth=self.bandwidth, time="0", number=str(current_number))
                current_number += 1
                yield url
                
        else:
            current_number = 1
            current_time = 0
            for s in timeline:
                if 't' in s:
                    current_time = s['t']
                for r in xrange(1+s['r']):
                    url = ProcessUrlTemplate(media, representation_id=self.id, bandwidth=self.bandwidth, time=str(current_time), number=str(current_number))
                    current_number += 1
                    current_time += s['d']
                    yield url
                        
    def GenerateSegmentUrlsFromList(self):
        segs = self.xml.find(DASH_NS+'SegmentList').findall(DASH_NS+'SegmentURL')
        for seg in segs:
            media = seg.get('media')
            if media is not None:
                yield media
        
    def __str__(self):
        result = "Representation: "
        return result

class DashAdaptationSet:
    def __init__(self, xml, parent):
        self.xml = xml
        self.parent = parent
        self.segment_base = DashSegmentBaseInfo(xml)
        self.representations = []
        for r in self.xml.findall(DASH_NS+'Representation'):
            self.representations.append(DashRepresentation(r, self))
        
    def __str__(self):
        result = 'Adaptation Set:\n' + '\n'.join([str (r) for r in self.representations])
        return result

class DashPeriod:
    def __init__(self, xml, parent):
        self.xml = xml
        self.parent = parent
        self.segment_base = DashSegmentBaseInfo(xml)
        self.adaptation_sets = []
        for s in self.xml.findall(DASH_NS+'AdaptationSet'):
            self.adaptation_sets.append(DashAdaptationSet(s, self))
        
    def __str__(self):
        result = 'Period:\n' + '\n'.join([str(s) for s in self.adaptation_sets])
        return result
        
class DashMPD:
    def __init__(self, url, xml):
        self.url = url
        self.xml = xml
        self.parent = None
        self.periods = []
        self.segment_base = DashSegmentBaseInfo(xml)
        self.type = xml.get('type')
        for p in self.xml.findall(DASH_NS+'Period'):
            self.periods.append(DashPeriod(p, self))
        
        # compute base URL (note: we'll just use the MPD URL for now)
        self.base_urls = [url] 
        base_url = self.xml.find(DASH_NS+'BaseURL')
        if base_url is not None:
            self.base_urls = [base_url.text]
        
    def __str__(self):
        result = "MPD:\n" + '\n'.join([str(p) for p in self.periods])
        return result

def ParseMpd(url, xml):
    mpd_tree = ElementTree.XML(xml)
    
    if mpd_tree.tag.startswith(DASH_NS_COMPAT):
        global DASH_NS
        global DASH_NS_URN
        DASH_NS = DASH_NS_COMPAT
        DASH_NS_URN = DASH_NS_URN_COMPAT
        if Options.verbose:
            print '@@@ Using backward compatible namespace'
            
    mpd = DashMPD(url, mpd_tree)
    if not (mpd.type is None or mpd.type == 'static'):
        raise Exception('Only static MPDs are supported')
        
    return mpd
        
def MakeNewDir(dir, force=False):
    if os.path.exists(dir):
        if force:
            print 'directory "'+dir+'" already exists, deleting with all it\'s content'
            shutil.rmtree(dir)
        else:
            print 'directory "'+dir+'" already exists'
            sys.exit(1)

    os.mkdir(dir)

def OpenURL(url):
    if url.startswith("file://"):
        return open(url[7:], 'rb')
    else:
        return urllib2.urlopen(url)

def ComputeUrl(base_url, url):
    if url.startswith('http://') or url.startswith('https://'):
        raise Exception('Absolute URLs are not supported')
    
    if base_url.startswith('file://'):
        return os.path.join(os.path.dirname(base_url), url)
    else:
        return urlparse.urljoin(base_url, url)
    
class Cloner:
    def __init__(self, root_dir):
        self.root_dir = root_dir
        self.track_ids = []
        self.init_filename = None
        
    def CloneSegment(self, url, path_out, is_init):
        while path_out.startswith('/'):
            path_out = path_out[1:]
        target_dir = os.path.join(self.root_dir, path_out)
        if Options.verbose:
            print 'Cloning', url, 'to', path_out
    
        #os.makedirs(target_dir)
        try:
            os.makedirs(os.path.dirname(target_dir))
        except OSError:
            if os.path.exists(target_dir):
                pass
        except:
            raise            
                
        data = OpenURL(url)
        outfile_name = os.path.join(self.root_dir, path_out)
        use_temp_file = False
        if Options.encrypt:
            use_temp_file = True
            outfile_name_final = outfile_name
            outfile_name += '.tmp'
        outfile = open(outfile_name, 'wb+')
        try:
            shutil.copyfileobj(data, outfile)
            outfile.close()
            
            if Options.encrypt:
                if is_init:
                    self.track_ids = GetTrackIds(outfile_name)
                    self.init_filename = outfile_name

                #shutil.copyfile(outfile_name, outfile_name_final)
                args = ["--method", "MPEG-CENC"]
                for t in self.track_ids:
                    args.append("--property")
                    args.append(str(t)+":KID:"+Options.kid.encode('hex'))
                for t in self.track_ids:
                    args.append("--key")
                    args.append(str(t)+":"+Options.key.encode('hex')+':random')
                args += [outfile_name, outfile_name_final]

                if not is_init:
                    args += ["--fragments-info", self.init_filename]
                    
                if Options.verbose:
                    print 'mp4encrypt '+(' '.join(args))
                Bento4Command("mp4encrypt", *args)
        finally:
            if use_temp_file and not is_init:
                os.unlink(outfile_name)
    
    def Cleanup(self):
        if (self.init_filename):
            os.unlink(self.init_filename)

class MpdSegment:
    def __init__(self, representation, segment_info, position = None):
        self.type = segment_info.tag
        
        segment_timeline_list = representation.parent.xml.find(DASH_NS+'SegmentList')
        timescale = segment_timeline_list.get('timescale')
        
        if self.type == DASH_NS+'SegmentURL' and position is not None:
            timeline_duration = segment_timeline_list.find(DASH_NS+'SegmentTimeline')[position].get('d')
            self.duration = float(timeline_duration) / float(timescale)
            self.url = segment_info.get('media')
        elif self.type == DASH_NS+'Initialization':
            self.duration = 0
            self.url = segment_info.get('sourceURL')

class MpdSource:
    def __init__(self, representation):
        self.id = representation.xml.get('id')
        self.base_url = representation.xml.find(DASH_NS+'BaseURL').text
        self.type = representation.parent.xml.get('mimeType')
        self.bandwidth = representation.xml.get('bandwidth')
        self.codecs = representation.xml.get('codecs')
        
        self.framerate = representation.xml.get('frameRate') or 0
        self.width = representation.xml.get('width') or None
        self.height = representation.xml.get('height') or None
        self.data_segments = []
        position = 0
        for segment in representation.xml.find(DASH_NS+'SegmentList'):
            if segment.tag == DASH_NS+'Initialization':
                self.init_segment = MpdSegment(representation, segment)
            elif segment.tag == DASH_NS+'SegmentURL':
                mpd_segment = MpdSegment(representation, segment, position)
                self.data_segments.append(mpd_segment)
                position += 1

def main():
    # determine the platform binary name
    host_platform = ''
    if platform.system() == 'Linux':
        if platform.processor() == 'x86_64':
            host_platform = 'linux-x86_64'
        else:
            host_platform = 'linux-x86'
    elif platform.system() == 'Darwin':
        host_platform = 'macosx'
    elif platform.system() == 'Windows':
        host_platform = 'win32'
    default_exec_dir = path.join(SCRIPT_PATH, 'bin', host_platform)
    if not path.exists(default_exec_dir):
        default_exec_dir = path.join(SCRIPT_PATH, 'bin')
    if not path.exists(default_exec_dir):
        default_exec_dir = path.join(SCRIPT_PATH, '..', 'bin')

    # parse options
    parser = OptionParser(usage="%prog [options] <file-or-http-url>\n")
    parser.add_option('-v', '--verbose', dest='verbose', action='store_true', default=False,
                      help='Be verbose')
    parser.add_option('-d', '--debug', dest='debug', default=False,
                      help='Print out debugging information')
    parser.add_option('-o', '--output-dir', dest='output_dir',
                      help='Output directory', metavar='<output-dir>', default='output')
    parser.add_option('-f', '--force', dest='force_output', action='store_true',
                      help='Allow output to an existing directory', default=False)
    parser.add_option('', '--exec-dir', metavar='<exec_dir>', dest='exec_dir', default=default_exec_dir,
                      help='Directory where the Bento4 executables are located')
    parser.add_option('', '--hls-master-playlist-name', dest='hls_master_playlist_name',
                      help='HLS master playlist name (default: master.m3u8)', metavar='<filename>', default='master.m3u8')
    parser.add_option('', '--use-urls', dest='use_urls', action='store_true', default=False,
                      help='Use an array of urls instead of a manifest')
    parser.add_option('', '--rename-media', dest='rename_media', action='store_true', default=False,
                      help = 'Use a file name pattern instead of the base name of input files for output media files.')
    parser.add_option('', '--media-prefix', dest='media_prefix', metavar='<prefix>', default='media',
                      help='Use this prefix for prefixed media file names (instead of the default prefix "media")')

    (options, args) = parser.parse_args()
    if len(args) != 1:
        parser.print_help()
        sys.exit(1)
    global Options
    Options = options

    # process arguments
    mpd_url = args[0]
    output_dir = options.output_dir

    # create the output dir
    MakeNewDir(options.output_dir, options.force_output)

    if Options.use_urls:
        mpd_sources = args[0].split(',')
        mp4tohls.GenerateHls(mpd_sources, options)
        sys.exit(0)

    # load and parse the MPD
    if Options.verbose: print "Loading MPD from", mpd_url
    try:
        mpd_xml = OpenURL(mpd_url).read()
    except Exception as e:
        print "ERROR: failed to load MPD:", e
        sys.exit(1)

    if Options.verbose: print "Parsing MPD"
    try:
        mpd_xml = mpd_xml.replace('nitialisation', 'nitialization')
        mpd = ParseMpd(mpd_url, mpd_xml)

        ElementTree.register_namespace('', DASH_NS_URN)
        ElementTree.register_namespace('mas', MARLIN_MAS_NS_URN)

        audio_sources = []
        video_sources = []
        
        for period in mpd.periods:
            for adaptation_set in period.adaptation_sets:
                for representation in adaptation_set.representations:
                    mpd_source = MpdSource(representation)
                    if mpd_source.type == 'audio/mp4':
                        audio_sources.append(mpd_source)
                    elif mpd_source.type == 'video/mp4':
                        video_sources.append(mpd_source)

        master_playlist_file = open(path.join(options.output_dir, options.hls_master_playlist_name), 'w+')
        master_playlist_file.write('#EXTM3U\r\n')
        master_playlist_file.write('# Created with Bento4 mp4-dash.py, VERSION=' + mp4tohls.VERSION + '-' + mp4tohls.SDK_REVISION+'\r\n')
        master_playlist_file.write('#\r\n')
        master_playlist_file.write('#EXT-X-VERSION:6\r\n')
        master_playlist_file.write('#EXT-X-INDEPENDENT-SEGMENTS\r\n')
        
        master_playlist_file.write('\r\n')
        master_playlist_file.write('# Media Playlists\r\n')
        
        master_playlist_file.write('\r\n')
        master_playlist_file.write('# Audio\r\n')

        for i, audio_source in enumerate(audio_sources):
            filename = audio_source.id + '.m3u8'
            media_playlist_file = open(path.join(options.output_dir, filename), 'w+')
            master_playlist_file.write(('#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud%d",LANGUAGE="en",NAME="English",BANDWIDTH=%d,AUTOSELECT=YES,DEFAULT=YES,URI="%s"\r\n' % (
                                       i,
                                       int(audio_source.bandwidth),
                                       filename)).encode('utf-8'))

            max_duration = int(math.ceil(max([value.duration for index, value in enumerate(audio_source.data_segments)])))

            media_playlist_file.write('#EXTM3U\r\n')
            media_playlist_file.write('#EXT-X-TARGETDURATION:%d\r\n' % (max_duration))
            media_playlist_file.write('#EXT-X-VERSION:6\r\n')
            media_playlist_file.write('#EXT-X-MEDIA-SEQUENCE:0\r\n')
            media_playlist_file.write('#EXT-X-PLAYLIST-TYPE:VOD\r\n')
            media_playlist_file.write(('#EXT-X-MAP:URI="%s"\r\n' % path.join(audio_source.base_url, audio_source.init_segment.url)).encode('utf-8'))

            bitrate = int(audio_source.bandwidth) / len(audio_source.data_segments)

            for data in audio_source.data_segments:
                media_playlist_file.write('#EXTINF:%f,\r\n' % (data.duration))
                media_playlist_file.write((path.join(audio_source.base_url, data.url)).encode('utf-8'))
                media_playlist_file.write('\r\n')
            media_playlist_file.write('#EXT-X-ENDLIST')

        master_playlist_file.write('\r\n')
        master_playlist_file.write('# Video\r\n')

        for j, video_source in enumerate(video_sources):
            filename = video_source.id + '.m3u8'
            media_playlist_file = open(path.join(options.output_dir, filename), 'w+')
            for i, audio_source in enumerate(audio_sources):
                master_playlist_file.write(('#EXT-X-STREAM-INF:AUDIO="aud%d",AVERAGE-BANDWIDTH=%d,BANDWIDTH=%d,CODECS="%s",RESOLUTION=%sx%s\r\n' % (
                                            i,
                                            int(video_source.bandwidth) + int(audio_source.bandwidth),
                                            int(video_source.bandwidth) + int(audio_source.bandwidth),
                                            video_source.codecs + ',' + audio_source.codecs,
                                            video_source.width,
                                            video_source.height)).encode('utf-8'))
                master_playlist_file.write(filename+'\r\n')
            
            max_duration = int(math.ceil(max([value.duration for index, value in enumerate(video_source.data_segments)])))

            media_playlist_file.write('#EXTM3U\r\n')
            media_playlist_file.write('#EXT-X-TARGETDURATION:%d\r\n' % (max_duration))
            media_playlist_file.write('#EXT-X-VERSION:6\r\n')
            media_playlist_file.write('#EXT-X-MEDIA-SEQUENCE:1\r\n')
            media_playlist_file.write('#EXT-X-PLAYLIST-TYPE:VOD\r\n')
            media_playlist_file.write(('#EXT-X-MAP:URI="%s"\r\n' % path.join(video_source.base_url, video_source.init_segment.url)).encode('utf-8'))

            bitrate = int(video_source.bandwidth) / len(video_source.data_segments)
            
            for data in video_source.data_segments:
                media_playlist_file.write('#EXTINF:%f,\r\n' % (data.duration))
                media_playlist_file.write((path.join(video_source.base_url, data.url)).encode('utf-8'))
                media_playlist_file.write('\r\n')
            media_playlist_file.write('#EXT-X-ENDLIST')
    except Exception as e:
        print "ERROR: Trying second method", e
        mpd_sources = GetMpdSources(mpd_xml)
        mp4tohls.GenerateHls(mpd_sources, options)

    
###########################    
SCRIPT_PATH = os.path.abspath(os.path.dirname(__file__))
if __name__ == '__main__':
    main()
    
