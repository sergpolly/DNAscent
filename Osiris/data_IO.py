#!/usr/bin/env python

#----------------------------------------------------------
# Copyright 2017 University of Oxford
# Written by Michael A. Boemo (michael.boemo@path.ox.ac.uk)
#----------------------------------------------------------

import numpy as np
import warnings
import h5py
import pysam
import re
from utility import reverseComplement


def import_reference(filename):
#	takes the filename of a fasta reference sequence and returns the reference sequence as a string.  N.B. the reference file must have only one sequence in it
#	ARGUMENTS
#       ---------
#	- filename: path to a reference fasta file
#	  type: string
#	OUTPUTS
#       -------
#	- reference: reference string
#	  type: string

	f = open(filename,'r')
	g = f.readlines()
	f.close()

	reference = ''
	for line in g:
		if line[0] != '>':
			reference += line.rstrip()
	g = None

	reference = reference.upper()

	if not all(c in ['A','T','G','C','N'] for c in reference):
		warnings.warn('Warning: Illegal character in reference.  Legal characters are A, T, G, C, and N.', Warning)

	return reference


def import_poreModel(filename):
#	takes the filename of an ONT pore model file and returns a map from kmer (string) to [mean,std] (list of floats)
#	ARGUMENTS
#       ---------
#	- filename: path to an ONT model file
#	  type: string
#	OUTPUTS
#       -------
#	- kmer2MeanStd: a map, keyed by a kmer, that returns the model mean and standard deviation signal for that kmer
#	  type: dictionary

	f = open(filename,'r')
	g = f.readlines()
	f.close()

	kmer2MeanStd = {}
	for line in g:
		if line[0] != '#' and line[0:4] != 'kmer': #ignore the header
			splitLine = line.split('\t')
			kmer2MeanStd[ splitLine[0] ] = [ float(splitLine[1]), float(splitLine[2]) ]
	g = None

	return kmer2MeanStd


def export_poreModel(emissions, outputFilename):
#	takes a dictionary of emissions produced by trainForAnalogue in train.py and outputs an ONT-style pore model file
#	ARGUMENTS
#       ---------
#	- emissions: keyed by a kmer string, outputs a list with kmer mean and standard deviation (generated by trainForAnalogue)
#	  type: dictionary
#	- outputFilename: path to the model file that should be created
#	  type: string
#	OUTPUTS
#       -------
#	- a model file is written to the directory specified

	#open the model file to write on
	f = open(outputFilename, 'w')

	#create header
	f.write('#model_name\ttemplate_median68pA.model.baseAnalogue\n')
	f.write('#type\tbase\n')
	f.write('#strand\ttemplate\n')
	f.write('#kit\tSQK007\n')
	f.write('kmer\tlevel_mean\tlevel_stdv\tsd_mean\tsd_stdv\n')

	#write kmer entries
	for key in emissions:
		toWrite = key+'\t'+str(emissions[key][0])+'\t'+str(emissions[key][1])+'\t0.0\t0.0\n'
		f.write(toWrite)

	#close the model file
	f.close()


def calculate_normalisedEvents(fast5Files, poreModelFile):
#	Uses a 5mer model to calculate the shift and scale pore-specific parameters for each individual read in a list of fast5 files
#	ARGUMENTS
#       ---------
#	- fast5Files: list of fast5 files whose events should be normalised
#	  type: list of strings
#	- poreModelFile: path to a pore model file.  This should be a 5mer model in the ONT format
#	  type: string
#	OUTPUTS
#       -------
#	- allNormalisedReads: a list, where each member is itself a list of events that have been normalised to the pore model
#	  type: list

	#open the 5mer model and make a map from the 5mer (string) to [mean,std] (list)
	kmer2MeanStd = import_poreModel(poreModelFile)

	#now iterate through all the relevant fast5 files so we only need to open the model file once
	allNormalisedReads = []
	for f5File in fast5Files:

		#use 5mer model to calculate shift, scale, drift, and var to normalise events for the pore
		f = h5py.File(f5File,'r')
		path = '/Analyses/Basecall_1D_000/BaseCalled_template/Events'
		Events = f[path]
		A = np.zeros((2,2))
		b = np.zeros((2,1))
		for event in Events:
			 if float(event[7]) > 0.30: #if there's a high probability (>30%) that the 5mer model called by Metrichor was the correct one
				model_5mer = event[4]
				event_mean = float(event[0])
				model_mean = kmer2MeanStd[model_5mer][0]
				model_std = kmer2MeanStd[model_5mer][1]
				
				#update matrix A
				A[0,0] += 1/(model_std**2)
				A[1,0] += 1/(model_std**2)*model_mean
				A[1,1] += 1/(model_std**2)*model_mean**2

				#update vector b
				b[0] += 1/(model_std**2)*event_mean
				b[1] += 1/(model_std**2)*event_mean*model_mean

		#use symmetry of A
		A[0,1] = A[1,0]

		#solve Ax = b to find shift and scale
		x = np.linalg.solve(A,b)
		shift = x[0][0]
		scale = x[1][0]

		#go through the same events as before and normalise them to the pore model using scale and shift
		normalisedEvents = []
		for event in Events:
			if float(event[7]) > 0.30: #if there's a high probability (>30%) that the 5mer model called by Metrichor was the correct one
				event_mean = float(event[0])
				normalisedEvents.append( event_mean/scale - shift)

		allNormalisedReads.append(normalisedEvents)

		f.close()
	
	return allNormalisedReads


def import_FixedPosTrainingData(readsFilename, reference, bamFile, poreModelFile):
#	Used to import training data from reads that have an analogue in a fixed context.
#	Creates a map from kmer (string) to a list of lists, where each list is comprised of events from a read
#	First reads a BAM file to see which reads (readIDs, sequences) aligned to the references based on barcoding.  Then finds the fast5 files
#	that they came from, normalises the events to a pore model, and returns the list of normalised events.
#	ARGUMENTS
#       ---------
#	- readsFilename: path to a fasta file of reads, generated by nanopolish extract
#	  type: string
#	- reference: path to a fasta reference file
#	  type: string
#	- bamFile: a BAM file from the alignment
#	  type: string
#	- poreModelFile: ONT model file for 5mers that can be used to normalised for shift and scale
#	  type: string
#	OUTPUTS
#       -------
#	- normalisedReads: a list of lists, where each element is a list of normalised events for a given read
#	  type: list


	#readsFilename is a fasta file of reads made by nanopolish extract
	f = open(readsFilename,'r')
	g = f.readlines()
	f.close()

	#make a map from readID to fast5 file path, which we'll need to find the fast5 file that corresponds to a record in the BAM file
	#note that this relies in nanopolish keeping the same format, and it may be good to make this more future-proof
	readIDMap = {}
	for line in g:
		if line[0] == '>':
			splitLine = line.split(' ')
			readIDMap[splitLine[0][1:]] = splitLine[2].rstrip()
	g = None

	#rest of this function makes a map from kmer (string) to a list of normalised events for that kmer
	kmer2Files = {}
	f = pysam.AlignmentFile(bamFile,'r')

	numRecords = f.count()
	print str(numRecords) + ' records in BAM file.'
	f = pysam.AlignmentFile(bamFile,'r')

	fast5files = []		
	for record in f:

		sequence = record.query_sequence
		readID = record.query_name

		fast5files.append(readIDMap[readID])

	f.close()

	normalisedReads = calculate_normalisedEvents(fast5files, poreModelFile)

	return normalisedReads


def import_HairpinTrainingData(readsFilename, reference, bamFile, poreModelFile, redundant_A_Loc, readsThreshold):
#	Used to import training data from a hairpin primer of the form 5'-...NNNBNNN....NNNANNN...-3'.
#	Creates a map from kmer (string) to a list of lists, where each list is comprised of events from a read
#	First reads a BAM file to see which reads (readIDs, sequences) aligned to the references based on barcoding.  Then finds the fast5 files
#	that they came from, normalises the events to a pore model, and returns the list of normalised events.
#	ARGUMENTS
#       ---------
#	- readsFilename: path to a fasta file of reads, generated by nanopolish extract
#	  type: string
#	- reference: path to a fasta reference file
#	  type: string
#	- bamFile: a BAM file from the alignment
#	  type: string
#	- poreModelFile: ONT model file for 5mers that can be used to normalised for shift and scale
#	  type: string
#	- redundant_A_Loc: location of the redundant A that is the reverse complement of BrdU (starting from 0)
#	  type: int
#	- readsThreshold: disregard a NNNANNN 7mer that only has a number of high quality reads below this threshold
#	  type: int
#	OUTPUTS
#       -------
#	- kmer2normalisedReads: a dictionary that takes a kmer string as a key and outputs a list of lists, where each list gives the normalised events from an individual read
#	  type: dictionary


	#readsFilename is a fasta file of reads made by nanopolish extract
	f = open(readsFilename,'r')
	g = f.readlines()
	f.close()

	#make a map from readID to fast5 file path, which we'll need to find the fast5 file that corresponds to a record in the BAM file
	#note that this relies in nanopolish keeping the same format, and it may be good to make this more future-proof
	readIDMap = {}
	for line in g:
		if line[0] == '>':
			splitLine = line.split(' ')
			readIDMap[splitLine[0][1:]] = splitLine[2].rstrip()
	g = None

	#rest of this function makes a map from kmer (string) to a list of normalised events for that kmer
	kmer2Files = {}
	f = pysam.AlignmentFile(bamFile,'r')

	numRecords = f.count()
	print str(numRecords) + ' records in BAM file.'
	f = pysam.AlignmentFile(bamFile,'r')

	for record in f:

		sequence = record.query_sequence
		readID = record.query_name

		#grab the part of the sequence that's flanked by start and end.  there may be more than one candidate.
		candidates = []
		start = reference[redundant_A_Loc-7:redundant_A_Loc-3] #four bases on the 5' end of the NNNANNN domain
		end = reference[redundant_A_Loc+4:redundant_A_Loc+8] #four bases on the 3' end of the NNNANNN domain
		start_indices = [s.start() for s in re.finditer('(?=' + start + ')', sequence)] #find all (possibly overlapping) indices of start using regular expressions
		end_indices = [s.start() for s in re.finditer('(?=' + end + ')', sequence)] #same for end
		for si in start_indices:
			si = si + len(start)
			for ei in end_indices:
				if ei > si:
					candidate = sequence[si:ei] #grab the subsequence between the start and end index
					if len(candidate) == 7 and candidate[3] == 'A': #consider it a candidate if it's a 7mer and has an A in the middle
						candidates.append(candidate)

		#only add the read to the map if we're sure that we've found exactly one correct redundant 7mer, and its reverse complement is in the sequence
		if len(candidates) == 1:
			idx_brdu = sequence.find(reverseComplement(candidates[0]))
			idx_a = sequence.find(candidates[0])
			if idx_brdu != -1 and idx_brdu < idx_a:
				if candidates[0] in kmer2Files:
					kmer2Files[candidates[0]] += [readIDMap[readID]]
				else:
					kmer2Files[candidates[0]] = [readIDMap[readID]]

	f.close()

	kmer2normalisedReads = {}
	counter = 0 #for progress
	for key in kmer2Files:

		if len(kmer2Files[key]) > readsThreshold: #if we have enough data to train on...
			#print progress	
			print 'Normalising for shift and scale... ' + str(float(counter)/float(len(kmer2Files))*100) + '%'

			normalisedReads = calculate_normalisedEvents(kmer2Files[key], poreModelFile)
			kmer2normalisedReads[key] = normalisedReads

		counter += 1

	return kmer2normalisedReads
