# Copyright 2017 Neural Networks and Deep Learning lab, MIPT
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from logging import getLogger
from typing import Dict, Any, List, Optional, Union

import numpy as np

from deeppavlov import Chainer
from deeppavlov.core.common.registry import register
from deeppavlov.core.models.component import Component
from deeppavlov.core.models.nn_model import NNModel
from deeppavlov.core.models.tf_model import LRScheduledTFModel
from deeppavlov.models.go_bot.data_handler import DataHandler
from deeppavlov.models.go_bot.nn_stuff_handler import NNStuffHandler
from deeppavlov.models.go_bot.tracker import FeaturizedTracker, DialogueStateTracker, MultipleUserStateTracker
from pathlib import Path

log = getLogger(__name__)


@register("go_bot")
class GoalOrientedBot(NNModel):
    """
    The dialogue bot is based on  https://arxiv.org/abs/1702.03274, which
    introduces Hybrid Code Networks that combine an RNN with domain-specific
    knowledge and system action templates.

    The network handles dialogue policy management.
    Inputs features of an utterance and predicts label of a bot action
    (classification task).

    An LSTM with a dense layer for input features and a dense layer for it's output.
    Softmax is used as an output activation function.

    Todo:
        add docstring for trackers.

    Parameters:
        tokenizer: one of tokenizers from
            :doc:`deeppavlov.models.tokenizers </apiref/models/tokenizers>` module.
        tracker: dialogue state tracker from
            :doc:`deeppavlov.models.go_bot.tracker </apiref/models/go_bot>`.
        hidden_size: size of rnn hidden layer.
        action_size: size of rnn output (equals to number of bot actions).
        dropout_rate: probability of weights dropping out.
        l2_reg_coef: l2 regularization weight (applied to input and output layer).
        dense_size: rnn input size.
        attention_mechanism: describes attention applied to embeddings of input tokens.

            * **type** – type of attention mechanism, possible values are ``'general'``, ``'bahdanau'``,
              ``'light_general'``, ``'light_bahdanau'``, ``'cs_general'`` and ``'cs_bahdanau'``.
            * **hidden_size** – attention hidden state size.
            * **max_num_tokens** – maximum number of input tokens.
            * **depth** – number of averages used in constrained attentions
              (``'cs_bahdanau'`` or ``'cs_general'``).
            * **action_as_key** – whether to use action from previous timestep as key
              to attention.
            * **intent_as_key** – use utterance intents as attention key or not.
            * **projected_align** – whether to use output projection.
        network_parameters: dictionary with network parameters (for compatibility with release 0.1.1,
            deprecated in the future)

        template_path: file with mapping between actions and text templates
            for response generation.
        template_type: type of used response templates in string format.
        word_vocab: vocabulary of input word tokens
            (:class:`~deeppavlov.core.data.simple_vocab.SimpleVocabulary` recommended).
        bow_embedder: instance of one-hot word encoder
            :class:`~deeppavlov.models.embedders.bow_embedder.BoWEmbedder`.
        embedder: one of embedders from
            :doc:`deeppavlov.models.embedders </apiref/models/embedders>` module.
        slot_filler: component that outputs slot values for a given utterance
            (:class:`~deeppavlov.models.slotfill.slotfill.DstcSlotFillingNetwork`
            recommended).
        intent_classifier: component that outputs intents probability
            distribution for a given utterance (
            :class:`~deeppavlov.models.classifiers.keras_classification_model.KerasClassificationModel`
            recommended).
        database: database that will be used during inference to perform
            ``api_call_action`` actions and get ``'db_result'`` result (
            :class:`~deeppavlov.core.data.sqlite_database.Sqlite3Database`
            recommended).
        api_call_action: label of the action that corresponds to database api call
            (it must be present in your ``template_path`` file), during interaction
            it will be used to get ``'db_result'`` from ``database``.
        use_action_mask: if ``True``, network output will be applied with a mask
            over allowed actions.
        debug: whether to display debug output.
    """

    def __init__(self,
                 tokenizer: Component,
                 tracker: FeaturizedTracker,
                 template_path: str,
                 save_path: str,
                 hidden_size: int = 128,
                 action_size: int = None,
                 dropout_rate: float = 0.,
                 l2_reg_coef: float = 0.,
                 dense_size: int = None,
                 attention_mechanism: dict = None,
                 network_parameters: Optional[Dict[str, Any]] = None,
                 load_path: str = None,
                 template_type: str = "DefaultTemplate",
                 word_vocab: Component = None,
                 bow_embedder: Component = None,
                 embedder: Component = None,
                 slot_filler: Component = None,
                 intent_classifier: Component = None,
                 database: Component = None,
                 api_call_action: str = None,
                 use_action_mask: bool = False,
                 debug: bool = False,
                 **kwargs) -> None:

        # todo навести порядок

        self.load_path = load_path
        self.save_path = save_path

        super().__init__(save_path=self.save_path, load_path=self.load_path, **kwargs)

        self.tokenizer = tokenizer  # preprocessing
        self.slot_filler = slot_filler  # another unit of pipeline
        self.intent_classifier = intent_classifier  # another unit of pipeline
        self.use_action_mask = use_action_mask  # feature engineering  todo: чот оно не на своём месте
        self.debug = debug

        self.data_handler = DataHandler(debug, template_path, template_type, word_vocab, bow_embedder, api_call_action,
                                        embedder)
        self.n_actions = len(self.data_handler.templates)  # upper-level model logic

        self.default_tracker = tracker  # tracker
        self.dialogue_state_tracker = DialogueStateTracker(tracker.slot_names, self.n_actions, hidden_size,
                                                           database)  # tracker

        self.intents = []
        if isinstance(self.intent_classifier, Chainer):
            self.intents = self.intent_classifier.get_main_component().classes  # upper-level model logic

        nn_stuff_save_path = Path(save_path, NNStuffHandler.SAVE_LOAD_SUBDIR_NAME)
        nn_stuff_load_path = Path(load_path, NNStuffHandler.SAVE_LOAD_SUBDIR_NAME)

        embedder_dim = self.data_handler.embedder.dim if self.data_handler.embedder else None
        use_bow_embedder = self.data_handler.use_bow_embedder()
        word_vocab_size = self.data_handler.word_vocab_size()

        self.nn_stuff_handler = NNStuffHandler(
            hidden_size,
            action_size,
            dropout_rate,
            l2_reg_coef,
            dense_size,
            attention_mechanism,
            network_parameters,
            embedder_dim,
            self.n_actions,
            self.intent_classifier,
            self.intents,
            self.default_tracker.num_features,
            use_bow_embedder,
            word_vocab_size,
            load_path=nn_stuff_load_path,
            save_path=nn_stuff_save_path,
            **kwargs)

        if self.nn_stuff_handler.train_checkpoint_exists():
            # todo переделать
            log.info(f"[initializing `{self.__class__.__name__}` from saved]")
            self.load()
        else:
            log.info(f"[initializing `{self.__class__.__name__}` from scratch]")

        self.multiple_user_state_tracker = MultipleUserStateTracker()  # tracker
        self.reset()  # tracker

    def _prepare_data(self, attn_hyperparams, x: List[dict], y: List[dict]) -> List[np.ndarray]:
        # region init vars
        b_features, b_u_masks, b_a_masks, b_actions = [], [], [], []
        b_emb_context, b_keys = [], []  # for attention
        # endregion init vars
        max_num_utter = max(len(d_contexts) for d_contexts in x)  # for padding
        for d_contexts, d_responses in zip(x, y):
            self.dialogue_state_tracker.reset_state()
            # region init dialogue_vars
            d_features, d_a_masks, d_actions = [], [], []
            d_emb_context, d_key = [], []  # for attention
            # endregion init dialogue vars

            for context, response in zip(d_contexts, d_responses):
                (action_id, utterance_action_mask,
                 utterance_attn_key, utterance_emb_context,
                 utterance_features) = self.process_utterance(attn_hyperparams, context, response)

                d_features.append(utterance_features)
                d_emb_context.append(utterance_emb_context)
                d_key.append(utterance_attn_key)
                d_a_masks.append(utterance_action_mask)
                d_actions.append(action_id)
                # update state
                # - previous action is teacher-forced here
                self.dialogue_state_tracker.update_previous_action(action_id)

                # region log mask
                if self.debug:
                    # log.debug(f"True response = '{response['text']}'.")
                    if d_a_masks[-1][action_id] != 1.:
                        pass
                        # log.warning("True action forbidden by action mask.")
                # endregion log mask

            # region padding to max_num_utter
            num_padds = max_num_utter - len(d_contexts)
            d_features.extend([np.zeros_like(d_features[0])] * num_padds)
            d_emb_context.extend([np.zeros_like(d_emb_context[0])] * num_padds)
            d_key.extend([np.zeros_like(d_key[0])] * num_padds)
            d_u_mask = [1] * len(d_contexts) + [0] * num_padds
            d_a_masks.extend([np.zeros_like(d_a_masks[0])] * num_padds)
            d_actions.extend([0] * num_padds)
            # endregion padding to max_num_utter

            # region extend batch with dialogue data
            b_features.append(d_features)
            b_emb_context.append(d_emb_context)
            b_keys.append(d_key)
            b_u_masks.append(d_u_mask)
            b_a_masks.append(d_a_masks)
            b_actions.append(d_actions)
            # endregion extend batch with dialogue data
        return b_features, b_emb_context, b_keys, b_u_masks, b_a_masks, b_actions

    def process_utterance(self, attn_hyperparams, context, response):
        utterance_tokens = self.tokenize_single_text_entry(context['text'])
        self.dialogue_state_tracker.update_ground_truth_db_result_from_context(context)
        if callable(self.slot_filler):
            context_slots = self.extract_slots_from_tokenized_text_entry(utterance_tokens)
            self.dialogue_state_tracker.update_state(context_slots)
        utterance_attn_key, utterance_features, utterance_emb_context = self.method_name(attn_hyperparams,
                                                                                         utterance_tokens)
        action_id = self.data_handler.encode_response(response['act'])
        utterance_action_mask = self.dialogue_state_tracker.calc_action_mask(self.data_handler.api_call_id)
        return action_id, utterance_action_mask, utterance_attn_key, utterance_emb_context, utterance_features

    def method_name(self, attn_hyperparams, tokens):
        intent_features = []
        if callable(self.intent_classifier):
            intent_features = self.extract_intents_from_tokenized_text_entry(tokens)

        tracker_prev_action = self.dialogue_state_tracker.prev_action

        state_features = self.dialogue_state_tracker.get_features()

        context_features = self.calc_context_features(self.dialogue_state_tracker.current_db_result,
                                                      self.dialogue_state_tracker.db_result,
                                                      self.dialogue_state_tracker.get_state())


        bow_features = []
        if self.data_handler.use_bow_embedder():
            bow_features = self.data_handler.bow_encode_tokens(tokens)

        attn_key, emb_context, emb_features = self.calc_attn_stuff(tokens, attn_hyperparams,
                                                                   intent_features, tracker_prev_action)

        concat_feats = np.hstack((bow_features, emb_features,
                                  intent_features, state_features,
                                  context_features, tracker_prev_action))

        return attn_key, concat_feats, emb_context

    def calc_attn_stuff(self, tokens, attn_hyperparams, intent_features, tracker_prev_action):
        emb_features = []
        emb_context = np.array([], dtype=np.float32)
        if attn_hyperparams:
            emb_context = self.calc_emb_context(attn_hyperparams, tokens)
        else:
            emb_features = self.calc_tokens_embedding(tokens)
        attn_key = np.array([], dtype=np.float32)
        if attn_hyperparams:
            attn_key = self.calc_attn_key(attn_hyperparams, intent_features, tracker_prev_action)
        return attn_key, emb_context, emb_features

    def calc_tokens_embedding(self, tokens):
        #todo to data
        emb_features = self.data_handler.embed_tokens(tokens, True)
        # random embedding instead of zeros
        if np.all(emb_features < 1e-20):
            emb_features = np.fabs(self.standard_normal_like(emb_features))
        return emb_features

    def calc_emb_context(self, attn_hyperparams, tokens):
        tokens_embedded = self.data_handler.embed_tokens(tokens, False)
        if tokens_embedded is not None:
            emb_context = self.calc_attn_context(attn_hyperparams, tokens_embedded)
        else:
            emb_context = np.array([], dtype=np.float32)
        return emb_context

    def standard_normal_like(self, source_vector):
        vector_dim = source_vector.shape[0]
        return np.random.normal(0, 1 / vector_dim, vector_dim)

    def calc_context_features(selfs, current_db_result, db_result, dst_state):
        result_matches_state = 0.
        if current_db_result is not None:
            matching_items = dst_state.items()
            result_matches_state = all(v == db_result.get(s)
                                       for s, v in matching_items
                                       if v != 'dontcare') * 1.
        context_features = np.array([
            bool(current_db_result) * 1.,
            (current_db_result == {}) * 1.,
            (db_result is None) * 1.,
            bool(db_result) * 1.,
            (db_result == {}) * 1.,
            result_matches_state
        ], dtype=np.float32)
        return context_features

    def calc_attn_key(self, attn_hyperparams, intent_features, tracker_prev_action):
        attn_key = np.array([], dtype=np.float32)

        if attn_hyperparams.use_action_as_key:
            attn_key = np.hstack((attn_key, tracker_prev_action))
        if attn_hyperparams.use_intent_as_key:
            attn_key = np.hstack((attn_key, intent_features))
        if len(attn_key) == 0:
            attn_key = np.array([1], dtype=np.float32)
        return attn_key

    def calc_attn_context(self, attn_hyperparams, tokens_embedded):
        # emb_context = calc_a
        padding_length = attn_hyperparams.window_size - len(tokens_embedded)
        padding = np.zeros(shape=(padding_length, attn_hyperparams.token_size), dtype=np.float32)
        if tokens_embedded:
            emb_context = np.concatenate((padding, np.array(tokens_embedded)))
        else:
            emb_context = padding
        return emb_context

    def extract_intents_from_tokenized_text_entry(self, tokens):
        intent_features = self.intent_classifier([' '.join(tokens)])[1][0]
        if self.debug:
            # todo log in intents extractor
            intent = self.intents[np.argmax(intent_features[0])]
            # log.debug(f"Predicted intent = `{intent}`")
        return intent_features

    def extract_slots_from_tokenized_text_entry(self, tokens):
        return self.slot_filler([tokens])[0]

    def tokenize_single_text_entry(self, x):
        return self.tokenizer([x.lower().strip()])[0]

    def train_on_batch(self, x: List[dict], y: List[dict]) -> dict:
        attn_hyperparams = self.nn_stuff_handler.get_attn_hyperparams()
        b_features, b_emb_context, b_keys, b_u_masks, b_a_masks, b_actions = self._prepare_data(attn_hyperparams, x, y)
        return self.nn_stuff_handler._network_train_on_batch(b_features, b_emb_context, b_keys, b_u_masks, b_a_masks,
                                                             b_actions)

    # todo как инфер понимает из конфига что ему нужно. лёша что-то говорил про дерево
    def _infer(self, tokens: List[str], tracker: DialogueStateTracker) -> List:

        attn_hyperparams = self.nn_stuff_handler.get_attn_hyperparams()
        attn_key, concat_feats, emb_context = self.method_name(attn_hyperparams, tokens)

        action_mask = tracker.calc_action_mask(self.data_handler.api_call_id)
        probs, state_c, state_h = \
            self.nn_stuff_handler._network_call([[concat_feats]], [[emb_context]], [[attn_key]],
                                                [[action_mask]], [[tracker.network_state[0]]],
                                                [[tracker.network_state[1]]],
                                                prob=True)  # todo чо за warning кидает ide, почему
        return probs, np.argmax(probs), (state_c, state_h)

    def _infer_dialog(self, contexts: List[dict]) -> List[str]:
        res = []
        self.dialogue_state_tracker.reset_state()
        for context in contexts:
            if context.get('prev_resp_act') is not None:
                previous_act_id = self.data_handler.encode_response(context['prev_resp_act'])
                # previous action is teacher-forced
                self.dialogue_state_tracker.update_previous_action(previous_act_id)

            # todo это ответ бд тоже teacher forced?
            tokens = self.tokenize_single_text_entry(context['text'])  # todo поч хардкодим ловеркейс

            self.dialogue_state_tracker.update_ground_truth_db_result_from_context(context)

            if callable(self.slot_filler):
                utter_slots = self.extract_slots_from_tokenized_text_entry(tokens)
                self.dialogue_state_tracker.update_state(utter_slots)

            _, predicted_act_id, self.dialogue_state_tracker.network_state = self._infer(tokens, tracker=self.dialogue_state_tracker)

            self.dialogue_state_tracker.update_previous_action(predicted_act_id)
            resp = self.data_handler.decode_response(predicted_act_id, self.dialogue_state_tracker)
            res.append(resp)
        return res

    def __call__(self,
                 batch: Union[List[dict], List[str]],
                 user_ids: Optional[List] = None) -> List[str]:
        # batch is a list of utterances
        if isinstance(batch[0], str):
            res = []
            if not user_ids:
                user_ids = ['finn'] * len(batch)
            for user_id, x in zip(user_ids, batch):
                if not self.multiple_user_state_tracker.check_new_user(user_id):
                    self.multiple_user_state_tracker.init_new_tracker(user_id, self.dialogue_state_tracker)

                tracker = self.multiple_user_state_tracker.get_user_tracker(user_id)
                tokens = self.tokenize_single_text_entry(x)

                if callable(self.slot_filler):
                    utter_slots = self.extract_slots_from_tokenized_text_entry(tokens)
                    tracker.update_state(utter_slots)

                _, predicted_act_id, tracker.network_state = self._infer(tokens, tracker=tracker)

                tracker.update_previous_action(predicted_act_id)

                # if made api_call, then respond with next prediction
                if predicted_act_id == self.data_handler.api_call_id:
                    tracker.make_api_call()

                    _, predicted_act_id, tracker.network_state = \
                        self._infer(tokens, tracker=tracker)

                    tracker.update_previous_action(predicted_act_id)

                resp = self.data_handler.decode_response(predicted_act_id, tracker)
                res.append(resp)
            return res
        # batch is a list of dialogs, user_ids ignored
        # todo: что значит коммент выше, почему узер идс игноред
        return [self._infer_dialog(x) for x in batch]

    def reset(self, user_id: Union[None, str, int] = None) -> None:
        # todo а чо, у нас всё что можно закешить лежит в мультиюхертрекере?
        self.multiple_user_state_tracker.reset(user_id)
        if self.debug:
            log.debug("Bot reset.")

    # region helping stuff
    def load(self, *args, **kwargs) -> None:
        self.nn_stuff_handler.load()
        super().load(*args, **kwargs)

    def save(self, *args, **kwargs) -> None:
        super().save(*args, **kwargs)
        self.nn_stuff_handler.save()

    def process_event(self, event_name, data) -> None:
        # todo что это
        super().process_event(event_name, data)

    # endregion helping stuff
