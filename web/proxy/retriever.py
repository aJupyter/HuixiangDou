# Copyright (c) OpenMMLab. All rights reserved.
"""extract feature and search with user query."""
import argparse
import json
import os
import re
import shutil
from pathlib import Path

import numpy as np
import pytoml
from BCEmbedding.tools.langchain import BCERerank
from file_operation import FileOperation
from langchain.embeddings import HuggingFaceEmbeddings
from langchain.retrievers import ContextualCompressionRetriever
from langchain.text_splitter import (MarkdownHeaderTextSplitter,
                                     MarkdownTextSplitter,
                                     RecursiveCharacterTextSplitter)
from langchain.vectorstores.faiss import FAISS as Vectorstore
from langchain_community.vectorstores.utils import DistanceStrategy
from langchain_core.documents import Document
from loguru import logger
from sklearn.metrics import precision_recall_curve
from torch.cuda import empty_cache
from helper import QueryTracker

class Retriever:
    """Tokenize and extract features from the project's documents, for use in
    the reject pipeline and response pipeline."""

    def __init__(self, embeddings, reranker, work_dir: str,
                 reject_throttle: float) -> None:
        """Init with model device type and config."""
        self.reject_throttle = reject_throttle
        self.rejecter = Vectorstore.load_local(os.path.join(
            work_dir, 'db_reject'),
            embeddings=embeddings, allow_dangerous_deserialization=True)
        self.retriever = Vectorstore.load_local(
            os.path.join(work_dir, 'db_response'),
            embeddings=embeddings, allow_dangerous_deserialization=True,
            distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT).as_retriever(
                search_type='similarity',
                search_kwargs={
                    'score_threshold': 0.15,
                    'k': 30
                })
        self.compression_retriever = ContextualCompressionRetriever(
            base_compressor=reranker, base_retriever=self.retriever)

    def is_reject(self, question, k=30, disable_throttle=False):
        """If no search results below the threshold can be found from the
        database, reject this query."""
        if disable_throttle:
            # for searching throttle during update sample
            docs_with_score = self.rejecter.similarity_search_with_relevance_scores(
                question, k=1)
            if len(docs_with_score) < 1:
                return True, docs_with_score
            return False, docs_with_score
        else:
            # for retrieve result
            # if no chunk passed the throttle, give the max
            docs_with_score = self.rejecter.similarity_search_with_relevance_scores(
                question, k=k)
            ret = []
            max_score = -1
            top1 = None
            for (doc, score) in docs_with_score:
                if score >= self.reject_throttle:
                    ret.append(doc)
                if score > max_score:
                    max_score = score
                    top1 = (doc, score)
            reject = False if len(ret) > 0 else True
            return reject, [top1]

    def update_throttle(self,
                        config_path: str = 'config.ini',
                        good_questions=[],
                        bad_questions=[]):
        """Update reject throttle based on positive and negative examples."""

        if len(good_questions) == 0 or len(bad_questions) == 0:
            raise Exception('good and bad question examples cat not be empty.')
        questions = good_questions + bad_questions
        predictions = []
        for question in questions:
            self.reject_throttle = -1
            _, docs = self.is_reject(question=question, disable_throttle=True)
            score = docs[0][1]
            predictions.append(max(0, score))

        labels = [1 for _ in range(len(good_questions))
                  ] + [0 for _ in range(len(bad_questions))]
        precision, recall, thresholds = precision_recall_curve(
            labels, predictions)

        # get the best index for sum(precision, recall)
        sum_precision_recall = precision[:-1] + recall[:-1]
        index_max = np.argmax(sum_precision_recall)

        optimal_threshold = max(thresholds[index_max], 0.0)

        with open(config_path, encoding='utf8') as f:
            config = pytoml.load(f)
        config['feature_store']['reject_throttle'] = float(optimal_threshold)
        with open(config_path, 'w', encoding='utf8') as f:
            pytoml.dump(config, f)
        logger.info(
            f'The optimal threshold is: {optimal_threshold}, saved it to {config_path}'  # noqa E501
        )

    def query(self,
              question: str,
              context_max_length: int = 16000,
              tracker: QueryTracker = None):
        """Processes a query and returns the best match from the vector store
        database. If the question is rejected, returns None.

        Args:
            question (str): The question asked by the user.

        Returns:
            str: The best matching chunk, or None.
            str: The best matching text, or None
        """
        if question is None or len(question) < 1:
            return None, None, []

        if len(question) > 512:
            logger.warning('input too long, truncate to 512')
            question = question[0:512]

        reject, docs = self.is_reject(question=question)
        assert (len(docs) > 0)
        if reject:
            return None, None, [docs[0][0].metadata['source']]

        docs = self.compression_retriever.get_relevant_documents(question)
        if tracker is not None:
            tracker.log('retrieve', [doc.metadata['source'] for doc in docs])
        chunks = []
        context = ''
        references = []

        # add file text to context, until exceed `context_max_length`

        file_opr = FileOperation()
        for idx, doc in enumerate(docs):
            chunk = doc.page_content
            chunks.append(chunk)

            if 'read' not in doc.metadata:
                logger.error(
                    'If you are using the version before 20240319, please rerun `python3 -m huixiangdou.service.feature_store`'
                )
                raise Exception('huixiangdou version mismatch')
            file_text, error = file_opr.read(doc.metadata['read'])
            if error is not None:
                # read file failed, skip
                continue

            source = doc.metadata['source']
            logger.info('target {} file length {}'.format(
                source, len(file_text)))
            if len(file_text) + len(context) > context_max_length:
                if source in references:
                    continue
                references.append(source)
                # add and break
                add_len = context_max_length - len(context)
                if add_len <= 0:
                    break
                chunk_index = file_text.find(chunk)
                if chunk_index == -1:
                    # chunk not in file_text
                    context += chunk
                    context += '\n'
                    context += file_text[0:add_len - len(chunk) - 1]
                else:
                    start_index = max(0, chunk_index - (add_len - len(chunk)))
                    context += file_text[start_index:start_index + add_len]
                break

            if source not in references:
                context += file_text
                context += '\n'
                references.append(source)

        assert (len(context) <= context_max_length)
        logger.debug('query:{} top1 file:{}'.format(question, references[0]))
        return '\n'.join(chunks), context, [
            os.path.basename(r) for r in references
        ]
