from typing import Dict

import numpy
from overrides import overrides

import torch
from torch.nn.modules.rnn import LSTMCell
from torch.nn.modules.linear import Linear
from allennlp.modules import FeedForward
import torch.nn.functional as F
from torch.nn.functional import cosine_similarity

from allennlp.common.util import START_SYMBOL, END_SYMBOL
from allennlp.data.vocabulary import Vocabulary
from allennlp.modules import TextFieldEmbedder, Seq2SeqEncoder
from allennlp.modules.attention import LegacyAttention
from allennlp.modules.similarity_functions import SimilarityFunction
from allennlp.modules.token_embedders import Embedding, TokenEmbedder
from allennlp.models.model import Model
from allennlp.nn.util import get_text_field_mask, sequence_cross_entropy_with_logits, weighted_sum, get_final_encoder_states


@Model.register("regr_seq2seq")
class RegrSeq2Seq(Model):
    """
    This ``Seq2Seq`` class is a :class:`Model` which takes a sequence, encodes it, and then
    uses the encoded representations to decode another sequence.  You can use this as the basis for
    a neural machine translation system, an abstractive summarization system, or any other common
    seq2seq problem.  The model here is simple, but should be a decent starting place for
    implementing recent models for these tasks.
    This ``Seq2Seq`` model takes an encoder (:class:`Seq2SeqEncoder`) as an input, and
    implements the functionality of the decoder.  In this implementation, the decoder uses the
    encoder's outputs in two ways. The hidden state of the decoder is initialized with the output
    from the final time-step of the encoder, and when using attention, a weighted average of the
    outputs from the encoder is concatenated to the inputs of the decoder at every timestep.
    Parameters
    ----------
    vocab : ``Vocabulary``, required
        Vocabulary containing source and target vocabularies. They may be under the same namespace
        (``tokens``) or the target tokens can have a different namespace, in which case it needs to
        be specified as ``target_namespace``.
    source_embedder : ``TextFieldEmbedder``, required
        Embedder for source side sequences
    encoder : ``Seq2SeqEncoder``, required
        The encoder of the "encoder/decoder" model
    max_decoding_steps : int, required
        Length of decoded sequences
    target_namespace : str, optional (default = 'tokens')
        If the target side vocabulary is different from the source side's, you need to specify the
        target's namespace here. If not, we'll assume it is "tokens", which is also the default
        choice for the source side, and this might cause them to share vocabularies.
    target_embedding_dim : int, optional (default = source_embedding_dim)
        You can specify an embedding dimensionality for the target side. If not, we'll use the same
        value as the source embedder's.
    attention_function: ``SimilarityFunction``, optional (default = None)
        If you want to use attention to get a dynamic summary of the encoder outputs at each step
        of decoding, this is the function used to compute similarity between the decoder hidden
        state and encoder outputs.
    scheduled_sampling_ratio: float, optional (default = 0.0)
        At each timestep during training, we sample a random number between 0 and 1, and if it is
        not less than this value, we use the ground truth labels for the whole batch. Else, we use
        the predictions from the previous time step for the whole batch. If this value is 0.0
        (default), this corresponds to teacher forcing, and if it is 1.0, it corresponds to not
        using target side ground truth labels.  See the following paper for more information:
        Scheduled Sampling for Sequence Prediction with Recurrent Neural Networks. Bengio et al.,
        2015.
    """
    def __init__(self,
                 vocab: Vocabulary,
                 source_embedder: TextFieldEmbedder,
                 encoder: Seq2SeqEncoder,
                 max_decoding_steps: int,
                 target_namespace: str = "tokens",
                 attention_function: SimilarityFunction = None,
                 scheduled_sampling_ratio: float = 0.0,
                 target_embedding_dim: int = None,
                 target_tokens_embedder: TokenEmbedder = None,
                 loss_type: str = None,
                 output_projection_layer: FeedForward = None
                 ) -> None:
        super(RegrSeq2Seq, self).__init__(vocab)
        self._source_embedder = source_embedder
        self._encoder = encoder
        self._max_decoding_steps = max_decoding_steps
        self._target_namespace = target_namespace
        self._attention_function = attention_function
        self._scheduled_sampling_ratio = scheduled_sampling_ratio
        # We need the start symbol to provide as the input at the first timestep of decoding, and
        # end symbol as a way to indicate the end of the decoded sequence.
        self._start_index = self.vocab.get_token_index(START_SYMBOL, self._target_namespace)
        self._end_index = self.vocab.get_token_index(END_SYMBOL, self._target_namespace)
        num_classes = self.vocab.get_vocab_size(self._target_namespace)
        # Decoder output dim needs to be the same as the encoder output dim since we initialize the
        # hidden state of the decoder with that of the final hidden states of the encoder. Also, if
        # we're using attention with ``DotProductSimilarity``, this is needed.
        self._decoder_output_dim = self._encoder.get_output_dim()
        
            
        
        # PRETRAINED PART
        if target_tokens_embedder:

            target_embedding_dim = target_tokens_embedder.get_output_dim()
            self._target_embedder = target_tokens_embedder 
            print('if', target_embedding_dim)
        else:
            target_embedding_dim = target_embedding_dim or self._source_embedder.get_output_dim()
            self._target_embedder = Embedding(num_classes, target_embedding_dim)

            print('else', target_embedding_dim)
        
        self._target_embedding_dim = target_embedding_dim
        print('after', self._target_embedding_dim, self._decoder_output_dim)
        
        if self._attention_function:
            self._decoder_attention = LegacyAttention(self._attention_function)
            # The output of attention, a weighted average over encoder outputs, will be
            # concatenated to the input vector of the decoder at each time step.
            self._decoder_input_dim = self._encoder.get_output_dim() + target_embedding_dim
        else:
            self._decoder_input_dim = target_embedding_dim
        # TODO (pradeep): Do not hardcode decoder cell type.
        self._decoder_cell = LSTMCell(self._decoder_input_dim, self._decoder_output_dim)
        # layer that predicts embedding vector for target
        if output_projection_layer:
            self._output_projection_layer = output_projection_layer
        else:
            self._output_projection_layer = Linear(self._decoder_output_dim, self._target_embedding_dim)
        
        self._loss_type = loss_type or "mse"

    @overrides
    def forward(self,  # type: ignore
                src_tokens: Dict[str, torch.LongTensor],
                tgt_tokens: Dict[str, torch.LongTensor] = None) -> Dict[str, torch.Tensor]:
        # pylint: disable=arguments-differ
        """
        Decoder logic for producing the entire target sequence.
        Parameters
        ----------
        src_tokens : Dict[str, torch.LongTensor]
           The output of ``TextField.as_array()`` applied on the source ``TextField``. This will be
           passed through a ``TextFieldEmbedder`` and then through an encoder.
        tgt_tokens : Dict[str, torch.LongTensor], optional (default = None)
           Output of ``Textfield.as_array()`` applied on target ``TextField``. We assume that the
           target tokens are also represented as a ``TextField``.
        """
        # (batch_size, input_sequence_length, encoder_output_dim)
        embedded_input = self._source_embedder(src_tokens)
        batch_size, _, _ = embedded_input.size()
        source_mask = get_text_field_mask(src_tokens)
        encoder_outputs = self._encoder(embedded_input, source_mask)
        final_encoder_output = get_final_encoder_states(encoder_outputs, source_mask, True)  # (batch_size, encoder_output_dim)
        if tgt_tokens:
            targets = tgt_tokens["tokens"]
            target_sequence_length = targets.size()[1]
            # The last input from the target is either padding or the end symbol. Either way, we
            # don't have to process it.
            num_decoding_steps = target_sequence_length - 1
        else:
            num_decoding_steps = self._max_decoding_steps
        decoder_hidden = final_encoder_output
        decoder_context = encoder_outputs.new_zeros(batch_size, self._decoder_output_dim)
        
        last_predicted_vector = None
        #step_logits = []
        step_predicted_vecs = []
        
        for timestep in range(num_decoding_steps):
            if self.training and torch.rand(1).item() >= self._scheduled_sampling_ratio:
                embedded_input_tgt = self._target_embedder(targets[:, timestep])
            else:
                if timestep == 0:
                    # For the first timestep, when we do not have targets, we input start symbols.
                    # (batch_size,)
                    embedded_input_tgt = self._target_embedder(source_mask.new_full((batch_size,), fill_value=self._start_index))
                else:
                    embedded_input_tgt = last_predicted_vector
            decoder_input = self._prepare_decode_step_input(embedded_input_tgt, decoder_hidden,
                                                            encoder_outputs, source_mask)
            decoder_hidden, decoder_context = self._decoder_cell(decoder_input,
                                                                 (decoder_hidden, decoder_context))
            # (batch_size, num_classes)
            #output_projections = self._output_projection_layer(decoder_hidden)
            
            # (batch_size, target_embedding_dim)
            predicted_targets_vecs = self._output_projection_layer(decoder_hidden)
            
            # list of (batch_size, 1, num_classes)
            step_predicted_vecs.append(predicted_targets_vecs.unsqueeze(1))
            #class_probabilities = F.softmax(output_projections, dim=-1)
            #_, predicted_classes = torch.max(class_probabilities, 1)
            #step_probabilities.append(class_probabilities.unsqueeze(1))
            last_predicted_vector = predicted_targets_vecs
            # (batch_size, 1)
            #step_predictions.append(last_predictions.unsqueeze(1))
        # step_predicted_vecs is a list containing tensors of shape (batch_size, 1, target_embedding_dim)
        # This is (batch_size, num_decoding_steps, num_classes)
        predicted_vecs = torch.cat(step_predicted_vecs, 1)
        #class_probabilities = torch.cat(step_probabilities, 1)
        #all_predictions = torch.cat(step_predictions, 1)
        output_dict = {"predicted_vecs": predicted_vecs}
        
        if tgt_tokens:
            target_mask = get_text_field_mask(tgt_tokens)
            loss = self._get_loss(predicted_vecs, targets, target_mask, self._target_embedder, self._loss_type)
            output_dict["loss"] = loss
            # TODO: Define metrics
        return output_dict

    def _prepare_decode_step_input(self,
                                   embedded_input: torch.LongTensor,
                                   decoder_hidden_state: torch.LongTensor = None,
                                   encoder_outputs: torch.LongTensor = None,
                                   encoder_outputs_mask: torch.LongTensor = None
                                   ) -> torch.LongTensor:
        """
        Given the input indices for the current timestep of the decoder, and all the encoder
        outputs, compute the input at the current timestep.  Note: This method is agnostic to
        whether the indices are gold indices or the predictions made by the decoder at the last
        timestep. So, this can be used even if we're doing some kind of scheduled sampling.
        If we're not using attention, the output of this method is just an embedding of the input
        indices.  If we are, the output will be a concatentation of the embedding and an attended
        average of the encoder inputs.
        Parameters
        ----------
        embedded_input : torch.LongTensor 
            Vectors of either the gold inputs to the decoder or the predicted vectors from the
            previous timestep. Shape: (batch_size, target_embedding_dim)
        decoder_hidden_state : torch.LongTensor, optional (not needed if no attention)
            Output of from the decoder at the last time step. Needed only if using attention.
        encoder_outputs : torch.LongTensor, optional (not needed if no attention)
            Encoder outputs from all time steps. Needed only if using attention.
        encoder_outputs_mask : torch.LongTensor, optional (not needed if no attention)
            Masks on encoder outputs. Needed only if using attention.
        """
        # input_indices : (batch_size,)  since we are processing these one timestep at a time.
        # (batch_size, target_embedding_dim)
        # embedded_input = self._target_embedder(input_indices)
        if self._attention_function:
            # encoder_outputs : (batch_size, input_sequence_length, encoder_output_dim)
            # Ensuring mask is also a FloatTensor. Or else the multiplication within attention will
            # complain.
            encoder_outputs_mask = encoder_outputs_mask.float()
            # (batch_size, input_sequence_length)
            input_weights = self._decoder_attention(decoder_hidden_state, encoder_outputs, encoder_outputs_mask)
            # (batch_size, encoder_output_dim)
            attended_input = weighted_sum(encoder_outputs, input_weights)
            # (batch_size, encoder_output_dim + target_embedding_dim)
            return torch.cat((attended_input, embedded_input), -1)
        else:
            return embedded_input

    @staticmethod
    def _get_loss(predicted_targets_vecs: torch.LongTensor,
                  target_ids: torch.LongTensor,
                  target_mask: torch.LongTensor,
                  target_embedder: TokenEmbedder,
                  loss_type) -> torch.LongTensor:
        """
        Takes predicted_targets_vecs (predicted based on decoder's outputs) of size (batch_size,
        num_decoding_steps, target_embedding_dim), target indices of size (batch_size, num_decoding_steps+1)
        and corresponding masks of size (batch_size, num_decoding_steps+1) steps and computes regression loss
        while taking the mask into account. The target_ids are embedded using pretrained embeddings by _target_embedder.
        
        
        The length of ``target_ids`` is expected to be greater than that of ``predicted_targets_vecs`` because the
        decoder does not need to compute the output corresponding to the last timestep of
        ``target_ids``. This method aligns the inputs appropriately to compute the loss.
        During training, we want the predicted_targets_vec corresponding to timestep i to be similar to the target
        token from timestep i + 1. That is, the targets should be shifted by one timestep for
        appropriate comparison.  Consider a single example where the target has 3 words, and
        padding is to 7 tokens.
           The complete sequence would correspond to <S> w1  w2  w3  <E> <P> <P>
           and the mask would be                     1   1   1   1   1   0   0
           and let the predicted_targets_vecs be     v1  v2  v3  v4  v5  v6
        We actually need to compare:
           the sequence           w1  w2  w3  <E> <P> <P>
           with masks             1   1   1   1   0   0
           against                v1  v2  v3  v4  v5  v6
           (where the input was)  <S> w1  w2  w3  <E> <P>
        """
        
        relevant_target_ids = target_ids[:, 1:].contiguous()  # (batch_size, num_decoding_steps)
        relevant_mask = target_mask[:, 1:].contiguous()  # (batch_size, num_decoding_steps)
        
        # embedd groundtruth targets with embedding module
        # shape: (batch_size, num_decoding_steps, target_embedding dim)
        embedded_golden_targets = target_embedder(relevant_target_ids) 
        
        #print("embedded_golden_targets.size():", embedded_golden_targets.size())
        #print("predicted_targets_vecs.size():", predicted_targets_vecs.size())
        #print("relevant_mask.size():", relevant_mask.size())
        
        # mask stuff
        relevant_mask = relevant_mask.unsqueeze(2).float() 
        embedded_golden_targets = embedded_golden_targets * relevant_mask
        predicted_targets_vecs = predicted_targets_vecs * relevant_mask
        
        loss = None
        if loss_type == "mse":
            loss = ((embedded_golden_targets - predicted_targets_vecs) ** 2).mean()
            # loss = ((embedded_golden_targets.detach() - predicted_targets_vecs) ** 2).mean()
        elif loss_type == "cos":
            sim_scores = cosine_similarity(embedded_golden_targets, predicted_targets_vecs, dim=2)
            I = torch.ones_like(sim_scores)
            loss = I - sim_scores
            loss = loss.mean()
        else:
            raise ValueError(loss_type + " is not implemented. Choose e.g. mse instead ")
            
        return loss

#     @overrides
#     def decode(self, output_dict: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
#         """
#         This method overrides ``Model.decode``, which gets called after ``Model.forward``, at test
#         time, to finalize predictions. The logic for the decoder part of the encoder-decoder lives
#         within the ``forward`` method.
#         This method trims the output predictions to the first end symbol, replaces indices with
#         corresponding tokens, and adds a field called ``predicted_tokens`` to the ``output_dict``.
#         """
# #         predicted_indices = output_dict["predictions"]
# #         if not isinstance(predicted_indices, numpy.ndarray):
# #             predicted_indices = predicted_indices.detach().cpu().numpy()
# #         all_predicted_tokens = []
# #         for indices in predicted_indices:
# #             indices = list(indices)
# #             # Collect indices till the first end_symbol
# #             if self._end_index in indices:
# #                 indices = indices[:indices.index(self._end_index)]
# #             predicted_tokens = [self.vocab.get_token_from_index(x, namespace=self._target_namespace)
# #                                 for x in indices]
# #             all_predicted_tokens.append(predicted_tokens)
# #         output_dict["predicted_vecs"] = all_predicted_tokens
#         return output_dict