from contextlib import closing
from PIL import Image
import subprocess
from audiotsm import phasevocoder
from audiotsm.io.wav import WavReader, WavWriter
from scipy.io import wavfile
import numpy as np
import re
import math
from shutil import copyfile, rmtree
import os
import argparse
from pytube import YouTube


# ===== Functions ======================================================================================================
# Download youtube video
def downloadFile(url):
    name = YouTube(url).streams.first().download()
    newname = name.replace(' ', '_')
    os.rename(name, newname)
    return newname


# Returns highest absolut magnitude
def getMaxVolume(s):
    maxv = float(np.max(s))
    minv = float(np.min(s))
    return max(maxv, -minv)


# Copies a single frame
def copyFrame(inputFrame, outputFrame):
    src = TEMP_FOLDER + "/frame{:06d}".format(inputFrame + 1) + ".jpg"
    dst = TEMP_FOLDER + "/newFrame{:06d}".format(outputFrame + 1) + ".jpg"
    if not os.path.isfile(src):
        return False
    copyfile(src, dst)
    if outputFrame % 20 == 19:
        print(str(outputFrame + 1) + " time-altered frames saved.")
    return True


# Appends "_ALTERED" to filename string
def inputToOutputFilename(filename):
    dotIndex = filename.rfind(".")
    return filename[:dotIndex] + "_ALTERED" + filename[dotIndex:]


# Creates a new folder at given path
def createPath(s):
    # assert (not os.path.exists(s)), "The filepath "+s+" already exists. Don't want to overwrite it. Aborting."
    try:
        os.mkdir(s)
    except OSError:
        assert False, "Creation of the directory %s failed. (The TEMP folder may already exist. Delete or rename it, " \
                      "and try again.) "


# Deletes given path
def deletePath(s):  # TODO: Dangerous! Watch out!
    try:
        rmtree(s, ignore_errors=False)
    except OSError:
        print("Deletion of the directory %s failed" % s)
        print(OSError)


# ===== Command line arguments & variables =============================================================================
parser = argparse.ArgumentParser(
    description='Modifies a video file to play at different speeds when there is sound vs. silence.')
parser.add_argument('--input_file', type=str, help='the video file you want modified')
parser.add_argument('--url', type=str, help='A youtube url to download and process')
parser.add_argument('--output_file', type=str, default="",
                    help="the output file. (optional. if not included, it'll just modify the input file name)")
parser.add_argument('--silent_threshold', type=float, default=0.03,
                    help="the volume amount that frames' audio needs to surpass to be consider \"sounded\". It ranges "
                         "from 0 (silence) to 1 (max volume)")
parser.add_argument('--sounded_speed', type=float, default=1.00,
                    help="the speed that sounded (spoken) frames should be played at. Typically 1.")
parser.add_argument('--silent_speed', type=float, default=5.00,
                    help="the speed that silent frames should be played at. 999999 for jump cutting.")
parser.add_argument('--frame_margin', type=float, default=1,
                    help="some silent frames adjacent to sounded frames are included to provide context. How many "
                         "frames on either the side of speech should be included? That's this variable.")
parser.add_argument('--sample_rate', type=float, default=44100, help="sample rate of the output video")
parser.add_argument('--frame_rate', type=float, default=30,
                    help="frame rate of the output video")
parser.add_argument('--frame_quality', type=int, default=3,
                    help="quality of frames to be extracted from input video. 1 is highest, 31 is lowest, 3 is the "
                         "default.")

args = parser.parse_args()

FRAME_RATE_OUT = args.frame_rate
SAMPLE_RATE_OUT = args.sample_rate
SILENT_THRESHOLD = args.silent_threshold
FRAME_SPREADAGE = args.frame_margin
NEW_SPEED = [args.silent_speed, args.sounded_speed]
if args.url is not None:
    INPUT_FILE = downloadFile(args.url)
else:
    INPUT_FILE = args.input_file
URL = args.url
FRAME_QUALITY = args.frame_quality

assert INPUT_FILE is not None, "why u put no input file, that dum"

if len(args.output_file) >= 1:
    OUTPUT_FILE = args.output_file
else:
    OUTPUT_FILE = inputToOutputFilename(INPUT_FILE)

TEMP_FOLDER = "TEMP"
# smooth out transitiion's audio by quickly fading in/out (arbitrary magic number whatever)
AUDIO_FADE_ENVELOPE_SIZE = 400

# ===== Start ==========================================================================================================
# Get frame rate
command = "ffprobe -v error -select_streams v:0 -show_entries stream=avg_frame_rate -of " \
          "default=noprint_wrappers=1:nokey=1 " + INPUT_FILE
stdout = subprocess.run(command, capture_output=True, text=True).stdout
print("Detected frame rate: " + str(stdout))
return_values = [int(val) for val in stdout.split('/')]
FRAME_RATE_IN = return_values[0] / return_values[1]

# Get audio sample rate
command = "ffprobe -v error -select_streams a:0 -show_entries stream=sample_rate -of " \
          "default=noprint_wrappers=1:nokey=1 " + INPUT_FILE
stdout = subprocess.run(command, capture_output=True, text=True).stdout
print("Detected sample rate: " + str(stdout))
SAMPLE_RATE_IN = int(stdout)

# TODO: Make the two independent
FRAME_RATE_OUT = FRAME_RATE_IN
#SAMPLE_RATE_OUT = SAMPLE_RATE_IN Using just a set bit rate seems to give better results

# Create temporary folder
createPath(TEMP_FOLDER)

# Extract the frames to the temp folder and set their quality via qscale
command = "ffmpeg -i " + INPUT_FILE + " -qscale:v " + str(
    FRAME_QUALITY) + " " + TEMP_FOLDER + "/frame%06d.jpg -hide_banner"
subprocess.call(command, shell=True)

# Extract the audio with a bitrate ("-ab") of 160K, two audio channels ("-ac 2"), and a bitrate ("-ar") given
# by the file itself. "-vn" disables any automatic mapping.
command = "ffmpeg -i " + INPUT_FILE + " -ab 160k -ac 2 -ar " +\
          str(SAMPLE_RATE_IN) + " -vn " + TEMP_FOLDER + "/audio.wav"
subprocess.call(command, shell=True)

# Read the extracted audio
audioData = wavfile.read(TEMP_FOLDER + "/audio.wav")[1]
audioSampleCount = audioData.shape[0]
maxAudioVolume = getMaxVolume(audioData)

# Various variables for audio processing
samplesPerFrame = SAMPLE_RATE_IN / FRAME_RATE_OUT
audioFrameCount = int(math.ceil(audioSampleCount / samplesPerFrame))
hasLoudAudio = np.zeros(audioFrameCount)

# Determine which audio chunks are loud enough
for i in range(audioFrameCount):
    start = int(i * samplesPerFrame)
    end = min(int((i + 1) * samplesPerFrame), audioSampleCount)
    audioChunks = audioData[start:end]
    maxChunksVolume = float(getMaxVolume(audioChunks)) / maxAudioVolume
    if maxChunksVolume >= SILENT_THRESHOLD:
        hasLoudAudio[i] = 1

chunks = [[0, 0, 0]]
shouldIncludeFrame = np.zeros(audioFrameCount)
for i in range(audioFrameCount):
    start = int(max(0, i - FRAME_SPREADAGE))
    end = int(min(audioFrameCount, i + 1 + FRAME_SPREADAGE))
    shouldIncludeFrame[i] = np.max(hasLoudAudio[start:end])
    if i >= 1 and shouldIncludeFrame[i] != shouldIncludeFrame[i - 1]:  # Did we flip?
        chunks.append([chunks[-1][1], i, shouldIncludeFrame[i - 1]])

chunks.append([chunks[-1][1], audioFrameCount, shouldIncludeFrame[i - 1]])
chunks = chunks[1:]

outputAudioData = np.zeros((0, audioData.shape[1]))
outputPointer = 0

lastExistingFrame = None
for chunk in chunks:
    audioChunk = audioData[int(chunk[0] * samplesPerFrame):int(chunk[1] * samplesPerFrame)]

    sFile = TEMP_FOLDER + "/tempStart.wav"
    eFile = TEMP_FOLDER + "/tempEnd.wav"
    wavfile.write(sFile, SAMPLE_RATE_OUT, audioChunk)
    with WavReader(sFile) as reader:
        with WavWriter(eFile, reader.channels, reader.samplerate) as writer:
            tsm = phasevocoder(reader.channels, speed=NEW_SPEED[int(chunk[2])])
            tsm.run(reader, writer)
    _, alteredAudioData = wavfile.read(eFile)
    leng = alteredAudioData.shape[0]
    endPointer = outputPointer + leng
    outputAudioData = np.concatenate((outputAudioData, alteredAudioData / maxAudioVolume))

    # outputAudioData[outputPointer:endPointer] = alteredAudioData/maxAudioVolume

    # smooth out transition's audio by quickly fading in/out
    if leng < AUDIO_FADE_ENVELOPE_SIZE:
        outputAudioData[outputPointer:endPointer] = 0  # audio is less than 0.01 sec, let's just remove it.
    else:
        premask = np.arange(AUDIO_FADE_ENVELOPE_SIZE) / AUDIO_FADE_ENVELOPE_SIZE
        mask = np.repeat(premask[:, np.newaxis], 2, axis=1)  # make the fade-envelope mask stereo
        outputAudioData[outputPointer:outputPointer + AUDIO_FADE_ENVELOPE_SIZE] *= mask
        outputAudioData[endPointer - AUDIO_FADE_ENVELOPE_SIZE:endPointer] *= 1 - mask

    startOutputFrame = int(math.ceil(outputPointer / samplesPerFrame))
    endOutputFrame = int(math.ceil(endPointer / samplesPerFrame))
    for outputFrame in range(startOutputFrame, endOutputFrame):
        inputFrame = int(chunk[0] + NEW_SPEED[int(chunk[2])] * (outputFrame - startOutputFrame))
        didItWork = copyFrame(inputFrame, outputFrame)
        if didItWork:
            lastExistingFrame = inputFrame
        else:
            copyFrame(lastExistingFrame, outputFrame)

    outputPointer = endPointer

wavfile.write(TEMP_FOLDER + "/audioNew.wav", SAMPLE_RATE_OUT, outputAudioData)

'''
outputFrame = math.ceil(outputPointer/samplesPerFrame)
for endGap in range(outputFrame,audioFrameCount):
    copyFrame(int(audioSampleCount/samplesPerFrame)-1,endGap)
'''

command = "ffmpeg -framerate " + str(
    FRAME_RATE_OUT) + " -i " + TEMP_FOLDER + "/newFrame%06d.jpg -i " + \
          TEMP_FOLDER + "/audioNew.wav -strict -2 " + OUTPUT_FILE
subprocess.call(command, shell=True)

deletePath(TEMP_FOLDER)
