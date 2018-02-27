'''
Build the pipeline workflow by plumbing the stages together.
'''

from ruffus import Pipeline, suffix, formatter, add_inputs, output_from, regex
from stages import Stages


def make_pipeline(state):
    '''Build the pipeline by constructing stages and connecting them together'''
    # Build an empty pipeline
    pipeline = Pipeline(name='haloplexpipe')
    # Get a list of paths to all the FASTQ files
    fastq_files = state.config.get_option('fastqs')
    # Stages are dependent on the state
    stages = Stages(state)

    # The original FASTQ files
    # This is a dummy stage. It is useful because it makes a node in the
    # pipeline graph, and gives the pipeline an obvious starting point.
    pipeline.originate(
        task_func=stages.original_fastqs,
        name='original_fastqs',
        output=fastq_files)

    pipeline.transform(
        task_func=stages.run_surecalltrimmer,
        name='run_surecalltrimmer',
        input=output_from('original_fastqs'),
        filter=formatter('.+/(?P<sample>[a-zA-Z0-9_-]+)_R1.fastq.gz'),
        add_inputs=add_inputs('{path[0]}/{sample[0]}_R2.fastq.gz'),
        extras=['{sample[0]}'],
        # output only needs to know about one file to track progress of the pipeline, but the second certainly exists after this step.
        output='processed_fastqs/{sample[0]}_R1.processed.fastq.gz')
    

    # Align paired end reads in FASTQ to the reference producing a BAM file
    pipeline.transform(
        task_func=stages.align_bwa,
        name='align_bwa',
        input=output_from('run_surecalltrimmer'),
        filter=formatter('processed_fastqs/(?P<sample>[a-zA-Z0-9_-]+)_R1.processed.fastq.gz'),
        add_inputs=add_inputs('processed_fastqs/{sample[0]}_R2.processed.fastq.gz'),
        extras=['{sample[0]}'],
        output='alignments/{sample[0]}.bam')

    #Run locatit from agilent.  this should produce sorted bam files, so no sorting needed at the next step
    pipeline.collate(
        task_func=stages.run_locatit,
        name='run_locatit',
        input=output_from('align_bwa', 'original_fastqs'),
        filter=formatter('.+/(?P<sample>[a-zA-Z0-9_-]+).+'),
        #filter=regex(r'.+/([a-zA-Z0-9_-]+).+'),
        #add_inputs=add_inputs(r'.+/\1_I2.fastq.gz'),
        output='alignments/{sample[0]}.locatit.bam',
        extras=['{sample[0]}']

#    filter=regex(r'.+/(.+BS\d{4,6}.+S\d+)\..+\.txt'),
#        output=r'all_sample.summary.\1.txt',
#        extras=[r'\1', 'all_sample.summary.txt'])







    # index bam file
    pipeline.transform(
        task_func=stages.index_sort_bam_picard,
        name='index_bam',
        input=output_from('run_locatit'),
        filter=suffix('.locatit.bam'),
        output='.locatit.bam.bai')

    # generate mapping metrics.
    pipeline.transform(
        task_func=stages.generate_amplicon_metrics,
        name='generate_amplicon_metrics',
        input=output_from('run_locatit'),
        filter=formatter('.+/(?P<sample>[a-zA-Z0-9_-]+).locatit.bam'),
        output='alignments/metrics/{sample[0]}.amplicon-metrics.txt',
        extras=['{sample[0]}'])

    pipeline.transform(
        task_func=stages.intersect_bed,
        name='intersect_bed',
        input=output_from('run_locatit'),
        filter=suffix('.locatit.bam'),
        output='.intersectbed.bam')

    pipeline.transform(
        task_func=stages.coverage_bed,
        name='coverage_bed',
        input=output_from('intersect_bed'),
        filter=suffix('.intersectbed.bam'),
        output='.bedtools_hist_all.txt')

    pipeline.transform(
        task_func=stages.genome_reads,
        name='genome_reads',
        input=output_from('run_locatit'),
        filter=suffix('.locatit.bam'),
        output='.mapped_to_genome.txt')

    pipeline.transform(
        task_func=stages.target_reads,
        name='target_reads',
        input=output_from('intersect_bed'),
        filter=suffix('.intersectbed.bam'),
        output='.mapped_to_target.txt')

    pipeline.transform(
        task_func=stages.total_reads,
        name='total_reads',
        input=output_from('run_locatit'),
        filter=suffix('.locatit.bam'),
        output='.total_raw_reads.txt')

    pipeline.collate(
        task_func=stages.generate_stats,
        name='generate_stats',
        input=output_from('coverage_bed', 'genome_reads', 'target_reads', 'total_reads'), 
        filter=regex(r'.+/(.+BS\d{4,6}.+S\d+)\..+\.txt'),
        output=r'all_sample.summary.\1.txt',
        extras=[r'\1', 'all_sample.summary.txt'])

    ###### GATK VARIANT CALLING ######
    # Call variants using GATK
    (pipeline.transform(
        task_func=stages.call_haplotypecaller_gatk,
        name='call_haplotypecaller_gatk',
        input=output_from('run_locatit'),
        filter=formatter('.+/(?P<sample>[a-zA-Z0-9-_]+).locatit.bam'),
        output='variants/gatk/{sample[0]}.g.vcf')
        .follows('index_sort_bam_picard'))

    # Combine G.VCF files for all samples using GATK
    pipeline.merge(
        task_func=stages.combine_gvcf_gatk,
        name='combine_gvcf_gatk',
        input=output_from('call_haplotypecaller_gatk'),
        output='variants/gatk/ALL.combined.vcf')

    # Genotype G.VCF files using GATK
    pipeline.transform(
        task_func=stages.genotype_gvcf_gatk,
        name='genotype_gvcf_gatk',
        input=output_from('combine_gvcf_gatk'),
        filter=suffix('.combined.vcf'),
        output='.raw.vcf')

    # Annotate VCF file using GATK
    pipeline.transform(
       task_func=stages.variant_annotator_gatk,
       name='variant_annotator_gatk',
       input=output_from('genotype_gvcf_gatk'),
       filter=suffix('.raw.vcf'),
       output='.raw.annotate.vcf')


#### split snps and indels for filtering ####

    pipeline.transform(
        task_func=stages.select_variants_snps_gatk,
        name='select_variants_snps_gatk',
        input=output_from('variant_annotator_gatk'),
        filter=suffix('raw.annotate.vcf'),
        output='raw.annotate.snps.vcf')

    pipeline.transform(
        task_func=stages.select_variants_indels_gatk,
        name='select_variants_indels_gatk',
        input=output_from('variant_annotator_gatk'),
        filter=suffix('raw.annotate.vcf'),
        output='raw.annotate.indels.vcf')

    pipeline.transform(
        task_func=stages.apply_variant_filtration_snps_gatk,
        name='apply_variant_filtration_snps_gatk',
        input=output_from('select_variants_snps_gatk'),
        filter=suffix('raw.annotate.snps.vcf'),
        output='raw.annotate.snps.filtered.vcf')

    pipeline.transform(
        task_func=stages.apply_variant_filtration_indels_gatk,
        name='apply_variant_filtration_indels_gatk',
        input=output_from('select_variants_indels_gatk'),
        filter=suffix('raw.annotate.indels.vcf'),
        output='raw.annotate.indels.filtered.vcf')

    (pipeline.transform(
        task_func=stages.merge_filtered_vcfs_gatk,
        name='merge_filtered_vcfs_gatk',
        input=output_from('apply_variant_filtration_snps_gatk'),
        filter=suffix('.raw.annotate.snps.filtered.vcf'),
        add_inputs=add_inputs(['variants/gatk/ALL.raw.annotate.indels.filtered.vcf']),
        output='.raw.annotate.filtered.merged.vcf')
        .follows('apply_variant_filtration_indels_gatk'))

    pipeline.transform(
        task_func=stages.left_align_split_multi_allelics,
        name="left_align_split_multi_allelics",
        input=output_from('merge_filtered_vcfs_gatk'),
        filter=suffix('.raw.annotate.filtered.merged.vcf'),
        output='.raw.annotate.filtered.merged.split_multi.vcf')

     #Apply VEP 
    (pipeline.transform(
        task_func=stages.apply_vep,
        name='apply_vep',
        input=output_from('left_align_split_multi_allelics'),
        filter=suffix('.raw.annotate.filtered.merged.split_multi.vcf'),
        add_inputs=add_inputs(['variants/undr_rover/combined_undr_rover.vcf.gz']),
        output='.raw.annotate.filtered.merged.split_multi.vep.vcf')
        .follows('left_align_split_multi_allelics'))


    return pipeline
