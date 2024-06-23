import csv
from tracemalloc import start
import numpy as np
import wave
from openai import audio
import soundfile as sf
from pathlib import Path
from read_ import *
# Define paths and file namesimport noisereduce as nr
import noisereduce as nr
import threading
import os
import shutil
import json

segment_duration = 3000
buff_ratio = 0.9
CURSE_WORD_FILE = 'curse_words.csv'

sample_audio_path = 'looperman.wav'
transcripts = ""
exports = ""
new_trans_path = Path.cwd()
new_trans_path = Path(str(new_trans_path) + "\\transcripts")

# 0.4 IS 0.2 ADDITIONAL SECONDS BEFORE AND AFTER CURSE ON TOP OF EXISTING SILENCE.
ADJUST_SILENCE = 1.25


class PortableNoiseReduction:
    def __init__(self, array: np.ndarray, start_time: float, end_time: float, sample_rate: int):
        """
        Initialize with a numpy array and specified start & end times for noise reduction.
        :param array: numpy array containing audio data.
        :param start_time: start time in seconds to apply noise reduction.
        :param end_time: end time in seconds to apply noise reduction.
        :param sample_rate: sample rate of the audio data in Hz.
        """
        self.array = array
        self.start_time = start_time if start_time > 0 else 0
        self.end_time = end_time
        self.sample_rate = sample_rate

    def apply_noise_reduction(self):
        """
        Apply noise reduction to the specified segment of the audio.
        Returns a new numpy array with noise reduction applied to the specified segment.
        """
        # Calculate the start and end indices
        start_sample = int(self.start_time * self.sample_rate)
        end_sample = int(self.end_time * self.sample_rate)

        # Extract segment for noise reduction
        segment = self.array[:,
                             start_sample:end_sample] if self.array.ndim == 2 else self.array[start_sample:end_sample]

        # Apply noise reduction
        reduced_noise_segment = nr.reduce_noise(y=segment, sr=self.sample_rate)

        # Replace original segment with reduced noise segment
        if self.array.ndim == 2:  # Multi-channel
            self.array[:, start_sample:end_sample] = reduced_noise_segment
        else:  # Single channel
            self.array[start_sample:end_sample] = reduced_noise_segment

        return self.array


def read_curse_words_from_csv(CURSE_WORD_FILE):
    """
     Read curse words from CSV file. This is a list of words that are part of CURIE's word list

     @param CURSE_WORD_FILE - Path to file to read

     @return List of words in CURIE's word list ( column A ) as defined in CSV file
    """
    curse_words_list = []
    with open(CURSE_WORD_FILE, newline='') as csvfile:
        reader = csv.reader(csvfile)
        for row in reader:
            # Assuming curse words are in column A
            curse_words_list.append(row[0])
    return curse_words_list


def load_wav_as_np_array(wav_file_path):
    """
     Load a WAV file and return the audio data as NumPy array. This function is used to load mono wav files that are stored in a file system.

     @param wav_file_path - The path to the WAV file

     @return A tuple containing the audio data and the sample
    """
    try:
        with wave.open(wav_file_path, "rb") as wav_file:
            # Ensure that the audio file is mono
            if wav_file.getnchannels() != 1:
                raise ValueError("Only mono audio files are supported.")

            # Extract audio frames
            frames = wav_file.readframes(wav_file.getnframes())

            # Convert audio frames to float64 NumPy array
            audio_data = np.frombuffer(
                frames, dtype=np.int16).astype(np.float32)

            # Normalize the audio data
            audio_data /= np.iinfo(np.int16).max

            # Return the audio data and the sample rate
            return audio_data, wav_file.getframerate()
    except wave.Error as e:
        print(f"An error occurred while reading the WAV file: {wav_file_path}")
        print(e)
    return sf.read(wav_file_path, dtype='float32')


def get_word_samples(word, sample_rate):
    """
    Get start and end sample indices from a word. This is a helper function for get_word_samples and get_word_samples_with_time_range.

    @param word - The word to get samples from. Should have'start'and'end'fields.
    @param sample_rate - The sample rate in Hz.

    @return A tuple of start and end sample indices for the word in time units of the sample_rate passed
    """
    start_time = word['start']
    end_time = word['end']

    # Convert the start and end times to sample indices
    start_sample = int(start_time * sample_rate)
    end_sample = int(end_time * sample_rate)

    return (start_sample, end_sample)


def apply_fade(audio_data, start_time, end_time, sample_rate, fade_duration=0.06):
    """
    Correctly apply a fade out at the start_time and a fade in at the end_time.
    """
    start_sample = int(start_time * sample_rate)
    end_sample = int(end_time * sample_rate)
    audio_length = len(audio_data)
    fade_samples = int(fade_duration * sample_rate)

    start_sample = max(0, min(start_sample, audio_length - 1))
    end_sample = max(0, min(end_sample, audio_length - 1))

    # Correcting the fade ranges according to the conventional definitions
    # Fade out before the start (reducing volume leading into the curse)
    fade_out_start = max(0, start_sample - fade_samples)
    fade_out_range = range(fade_out_start, start_sample)

    # Fade in after the end (increasing volume after the curse)
    fade_in_start = end_sample
    fade_in_range = range(fade_in_start, min(
        fade_in_start + fade_samples, audio_length))

    # Apply fade out
    for i, sample in enumerate(fade_out_range):
        fade_out_factor = i / len(fade_out_range)  # Linear fade
        audio_data[sample] *= fade_out_factor

    # Apply fade in
    for i, sample in enumerate(fade_in_range):
        fade_in_factor = 1 - (i / len(fade_in_range))  # Linear fade
        audio_data[sample] *= fade_in_factor

    fade_timestamps = {
        'fade_out_start': max(0, start_time - fade_duration),
        'fade_out_end': start_time,
        'fade_in_start': end_time,
        'fade_in_end': min(end_time + fade_duration, audio_length / sample_rate)
    }

    return audio_data, fade_timestamps


def split_silence(sample_rate, word, audio_data, nraudio=None):
    """
    Split silence into start and end indices with a buffer. Adjust the buffer duration as needed.

    @param sample_rate - Sampling rate of the sound in Hz.
    @param word - Word being split. Must contain 'start' and 'end' keys.
    @param buffer_duration - Additional duration in seconds to add as a buffer before and after the curse word.

    @return Tuple of start and end indices including the buffer. 
    """
    global buff_ratio
    # Example adjustment, ensure this is defined or adjusted as needed in your context.

    diff = word['end'] - word['start']
    end_time = (word['start'] + (diff * buff_ratio))
    start_time = (word['end'] - (diff * buff_ratio))

    # audio_data_faded, fade_dict = apply_fade(
    #     audio_data,
    #     start_time,
    #     end_time,
    #     sample_rate
    # )
    # start_time = fade_dict['fade_out_end']
    # end_time = fade_dict['fade_in_start']

    start_sample = max(0, int(start_time * sample_rate))
    end_sample = min(int(end_time * sample_rate), len(audio_data) - 1)
    if nraudio:
        nraudio.array = audio_data
        nraudio.start = start_time - 0.1 if start_time > 0.1 else 0
        nraudio.end = end_time + 0.1 if start_time + \
            0.1 > len(audio_data) else len(audio_data)
    return start_sample, end_sample, nraudio


def mute_curse_words(audio_data, sample_rate, transcription_result, curse_words_list):
    """
    Mute curse words in the audio data.
    """
    audio_data_muted = np.copy(audio_data)
    curse_words_set = set(word.lower() for word in curse_words_list)

    nraudio = PortableNoiseReduction(
        array=audio_data, start_time=0, end_time=0, sample_rate=sample_rate)

    for word in transcription_result:
        if word['word'].lower() in curse_words_set:
            start_sample, end_sample, nraudio = split_silence(
                sample_rate, word, audio_data_muted, nraudio)
            # Mute the section
            audio_data_muted[start_sample:end_sample] = 0
            nraudio = PortableNoiseReduction(
                array=audio_data, start_time=word['start'], end_time=word['end'], sample_rate=sample_rate)
            audio_data_muted = nraudio.apply_noise_reduction()
            # Apply fade in and fade out to the modified section
    return audio_data_muted


def convert_stereo(f):
    """
    Reads an audio file (.wav or .mp3) and returns it in a mono format.
    """
    return NumpyMono(f)


def find_curse_words(audio_content, sample_rate, results, CURSE_WORD_FILE=CURSE_WORD_FILE):
    """
     Find Curse words in audio content. This is a wrapper around mute_curse_words that takes into account the sample rate in order to get an accurate set of cursors and returns a set of words that are present in the audio

     @param audio_content - The audio content to search
     @param sample_rate - The sample rate in Hz
     @param transcript_file - The file containing the transcripts.
     @param CURSE_WORD_FILE - The path to the CSV file containing curse words.

     @return The set of words that are present in the audio content and are not present in the transcript. This set is used to make sure that we don't accidentally miss a word
    """
    curses = read_curse_words_from_csv(CURSE_WORD_FILE)
    curse_words_set = set(curses)
    return mute_curse_words(audio_content, sample_rate, results, curse_words_set)



def process_audio_batch(trans_audio):
    max_threads = 5
    threads = []
    processed_paths = {}

    def wait_for_threads(threads):
        for thread in threads:
            thread.join()
        threads.clear()

    threadnumb = 0
    for trans, audio in trans_audio.items():
        threadnumb += 1

        if len(threads) >= max_threads:
            wait_for_threads(threads)

        # Sample way to track processed paths. Implement according to the actual process_audio
        processed_paths[threadnumb] = f"{audio}_processed"

        thread = threading.Thread(
            target=process_audio, args=(audio, threadnumb, trans))
        threads.append(thread)
        thread.start()

    # Wait for the remaining threads
    wait_for_threads(threads)
    return processed_paths


def combine_wav_files(segment_paths):
    """
    Combines multiple .wav files into a single .wav file, ensuring the header information is correct.
    
    :param segment_paths: List of paths to .wav files to be combined.
    """
    if not segment_paths:
        print("No paths provided!")
        return

    output_nam = Path(segment_paths[0]).name
    output_path = Path(segment_paths[0]).parent / \
        f"{output_nam}combined_output.wav"

    with wave.open(str(output_path), 'wb') as output_wav:
        # Initialize parameters
        nchannels, sampwidth, framerate, nframes, comptype, compname = [None]*6
        # Read params from first file
        for segment_path in segment_paths:
            with wave.open(f"{segment_path}_clean_.wav", 'rb') as segment_wav:
                if not all([nchannels, sampwidth, framerate, comptype, compname]):
                    nchannels = segment_wav.getnchannels()
                    sampwidth = segment_wav.getsampwidth()
                    framerate = segment_wav.getframerate()
                    comptype = segment_wav.getcomptype()
                    compname = segment_wav.getcompname()
                    output_wav.setparams(
                        (nchannels, sampwidth, framerate, nframes, comptype, compname))
                output_wav.writeframes(
                    segment_wav.readframes(segment_wav.getnframes()))

    home = os.path.expanduser("~")
    # Construct the path to the user's download folder based on the OS
    download_folder = os.path.join(home, "Downloads")
    outfile_finished = os.path.join(
        download_folder, f"{output_nam}combined_output.wav")
    shutil.copyfile(output_path, outfile_finished)


def convert_json_format(input_filename, output_filename):
    """
    Converts a JSON file from a complex nested structure to a simplified
    structure focusing on words, their start, and end times.
    
    @param input_filename: Path to the input JSON file.
    @param output_filename: Path where the converted JSON is saved.
    """
    with open(input_filename, 'r', encoding='utf-8') as infile:
        data = json.load(infile)

    simplified_data = []
    for segment in data.get('segments', []):
        for word_info in segment.get('words', []):
            simplified_data.append({
                "word": word_info['word'].strip(r"',.\"-_/`?!; ").lower(),
                "start": word_info['start'],
                "end": word_info['end']
            })

    with open(output_filename, 'w', encoding='utf-8') as outfile:
        json.dump(simplified_data, outfile, indent=4)

    print(
        f'The data has been successfully converted and saved to: {output_filename}')
    return simplified_data


def process_audio(audio_file, transcript_file=None):
    """
     Process audio and transcribe it to wav. This is the main function of the program. It takes the audio file and transcribes it using transcript_file if it is not provided.

     @param audio_file - path to audio file to be transcribed
     @param transcript_file - path to transcript file. If not provided it will be transcribed

     @return path to audio file with processed
    """
    global processed_paths
    print('converting to stereo')
    print('reading audio')
    audio_obj = NumpyMono(audio_file)
    print('process json')
    results = convert_json_format(
        transcript_file, f'{transcript_file}_new.json')
    print('find curse words')
    audio_obj.np_array = find_curse_words(
        audio_obj.np_array, audio_obj.sample_rate, results)
    print('exporting file now....')
    audio_obj.numpy_to_wav()
